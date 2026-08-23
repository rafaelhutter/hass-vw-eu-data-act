"""Self-check for the portal session re-login fallback. Run: python3 tests/test_portal_relogin.py"""

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

# Load the module by path: importing the package would pull in homeassistant.
_SRC = Path(__file__).resolve().parents[1] / "custom_components/volkswagen_connect/website_portal.py"
_spec = importlib.util.spec_from_file_location("website_portal", _SRC)
wp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wp)

RELOGIN_COOLDOWN_S = wp.RELOGIN_COOLDOWN_S
WebsitePortalVehicleError = wp.WebsitePortalVehicleError
WebsitePortalAuthError = wp.WebsitePortalAuthError
WebsitePortalClient = wp.WebsitePortalClient
WebsitePortalConsentRequired = wp.WebsitePortalConsentRequired
_CONSENT_HINT = wp._CONSENT_HINT
_validate_landing = wp._validate_landing

PORTAL_OK = "https://www.volkswagen.de/de/besitzer-und-nutzer/myvolkswagen.html"
WebsitePortalError = wp.WebsitePortalError


class _FakeResponse:
    def __init__(self, status):
        self.status = status
        self.headers = {}

    async def text(self):
        return '{"error":{"code":4007}}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Answers every GET with one fixed status."""

    def __init__(self, status):
        self._status = status
        self.cookie_jar = ()

    def get(self, *args, **kwargs):
        return _FakeResponse(self._status)


def client(silent_error, login_result):
    c = WebsitePortalClient(None, "a@b.c", "pw")
    calls = {"login": 0}

    async def silent():
        if silent_error:
            raise silent_error

    async def begin_login():
        calls["login"] += 1
        if isinstance(login_result, Exception):
            raise login_result
        return login_result

    c._silent_refresh = silent
    c.begin_login = begin_login
    return c, calls


async def main():
    # landing validation
    _validate_landing(PORTAL_OK)
    for url, exc in (
        ("https://identity.vwgroup.io/v2/login/ui/consent", WebsitePortalConsentRequired),
        ("https://identity.vwgroup.io/u/login?state=x", WebsitePortalAuthError),
        (PORTAL_OK + "?error=login_required", WebsitePortalAuthError),
        ("https://example.com/", WebsitePortalAuthError),
    ):
        try:
            _validate_landing(url)
            raise AssertionError(f"expected raise for {url}")
        except exc:
            pass

    # healthy SSO -> no login attempt
    c, calls = client(None, "ok")
    await c.refresh()
    assert calls["login"] == 0

    # dead SSO -> silent credential re-login
    c, calls = client(WebsitePortalAuthError("SSO session expired"), "ok")
    await c.refresh()
    assert calls["login"] == 1

    # dead SSO + OTP demanded -> surface reauth
    c, calls = client(WebsitePortalAuthError("SSO session expired"), "otp_required")
    try:
        await c.refresh()
        raise AssertionError("expected reauth")
    except WebsitePortalAuthError as err:
        assert "OTP" in str(err), err
    assert calls["login"] == 1

    # consent wall -> never spend a login on it
    c, calls = client(WebsitePortalConsentRequired(_CONSENT_HINT), "ok")
    try:
        await c.refresh()
        raise AssertionError("expected consent error")
    except WebsitePortalConsentRequired:
        pass
    assert calls["login"] == 0

    # cooldown: a second failure inside the window must not re-hit the login
    c, calls = client(WebsitePortalAuthError("SSO session expired"), "ok")
    await c.refresh()
    try:
        await c.refresh()
        raise AssertionError("expected the cooled-down failure to surface")
    except WebsitePortalAuthError:
        pass
    assert calls["login"] == 1, calls
    with patch.object(wp.time, "monotonic",
                      return_value=c._last_relogin + RELOGIN_COOLDOWN_S + 1):
        await c.refresh()
    assert calls["login"] == 2, calls

    # _get classification: on a session the refresh just renewed, a repeated
    # 403/412 is VW refusing that vehicle — only 401 means the session is dead.
    for status, expected in ((403, WebsitePortalVehicleError),
                             (412, WebsitePortalVehicleError),
                             (428, WebsitePortalVehicleError),
                             (401, WebsitePortalAuthError)):
        c = WebsitePortalClient(_FakeSession(status), "a@b.c", "pw")

        async def ok():
            return None

        c.refresh = ok
        try:
            await c._get("/x")
            raise AssertionError(f"expected raise for {status}")
        except expected:
            pass
        except WebsitePortalError as err:
            raise AssertionError(f"{status} raised {type(err).__name__}") from err

    print("ok")


asyncio.run(main())
