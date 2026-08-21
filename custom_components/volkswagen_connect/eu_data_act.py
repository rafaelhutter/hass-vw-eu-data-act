"""Async client for the Volkswagen Connect portal.

No Home Assistant dependencies — only aiohttp + beautifulsoup4 — so it can be
unit-tested standalone. Ported from TA2k's ioBroker.vw-connect `lib/euDataAct.js`.

Why this exists: VW retired the WeConnect app OAuth client and now gates the
CARIAD BFF token exchange behind app attestation (Play Integrity), which an
open-source client cannot satisfy. The EU Data Act portal is the only remaining
attestation-free channel: a server-side confidential OAuth client (the portal)
delivers the vehicle's "continuous data" (15-min interval) per the EU Data Act.

Login is the classic VW Identity `signin-service` OIDC code flow with the
EU-Data-Act client id `9b58543e-…@apps_vw-dilab_com` and `redirect_uri` pointing
at the portal, so the portal performs the token exchange — no attestation,
and (observed) no MFA. Subsequent data calls ride the portal session cookie.

Caller responsibilities:
  * Pass an aiohttp.ClientSession with its OWN cookie jar (do not share one
    across integrations) — e.g. aiohttp.ClientSession(cookie_jar=CookieJar()).
  * The user must first, in a browser, accept consent on the portal, link the
    vehicle, and enable a continuous 15-min data request — otherwise the
    vehicle list / metadata is empty (EuDataActNotConfigured).
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
OIDC_AUTHORIZE_URL = f"{IDENTITY_BASE}/oidc/v1/authorize"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = f"{BASE_URL}/login"

# Brand -> OIDC client_id (verified live from the portal's brand selector).
BRAND_CLIENT_IDS = {
    "VOLKSWAGEN_PASSENGER_CARS": "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com",
    "VOLKSWAGEN_COMMERCIAL_VEHICLES": "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com",
    "AUDI": "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com",
    "BENTLEY": "d38aac0f-3d89-4a63-8538-b75b31322c7b@apps_vw-dilab_com",
    "SKODA": "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com",
    "SEAT": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
    "CUPRA": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
}
DEFAULT_BRAND = "VOLKSWAGEN_PASSENGER_CARS"

VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
RELATION_PATH = "/proxy_api/vum/v2/users/me/relations/{vin}"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"
NO_CONTENT_SUFFIX = "_no_content_found.zip"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class EuDataActError(Exception):
    """Base error."""


class EuDataActAuthError(EuDataActError):
    """Login failed (bad credentials, consent screen, throttled, …).

    ``reason`` is a config-flow error key so the UI can explain *why* instead of
    always showing "invalid email or password".
    """

    def __init__(self, message: str, reason: str = "invalid_auth") -> None:
        super().__init__(message)
        self.reason = reason


class EuDataActNotConfigured(EuDataActError):
    """No continuous data request is set up for the VIN on the portal."""


def _extract_template_model(html: str) -> dict:
    """Pull `window._IDK.templateModel = {…}` (hmac/relayState/email) by brace match."""
    idx = html.find("templateModel")
    if idx < 0:
        return {}
    brace = html.find("{", idx)
    if brace < 0:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _login_fields(html: str) -> tuple[dict, str | None]:
    """Extract the signin-service form fields (hidden inputs + hmac/relayState/_csrf)."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    fields: dict[str, str] = {}
    action: str | None = None
    if form:
        action = form.get("action")
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                fields[name] = inp.get("value", "")
    model = _extract_template_model(html)
    for key in ("hmac", "relayState"):
        if model.get(key):
            fields[key] = model[key]
    email = (model.get("emailPasswordForm") or {}).get("email")
    if email and "email" not in fields:
        fields["email"] = email
    if "_csrf" not in fields:
        m = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
        if m:
            fields["_csrf"] = m.group(1)
    return fields, action


def _login_error(html: str) -> str | None:
    model = _extract_template_model(html)
    err = model.get("error") or model.get("errorCode")
    if not err:
        return None
    if isinstance(err, dict):
        return err.get("text") or err.get("errorCode") or json.dumps(err)
    return str(err)


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested cluster JSON into dotted scalar keys, for sensor creation.

    Lists are collapsed to their *last* element under the same (index-free) key
    rather than expanded by position. The EU Data Act "continuous data" payload
    is largely time-series — a growing list of timestamped samples — so indexed
    keys (``foo[0]``, ``foo[1]``, …) would change on every delivery and spawn an
    unbounded stream of brand-new sensors. Collapsing to the latest sample keeps
    one stable key per signal (its most recent value), which is what a sensor
    should track anyway.
    """
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list):
        if data:
            out.update(flatten(data[-1], prefix))
    elif prefix:
        out[prefix] = data
    return out


# Envelope/metadata fields in the EU Data Act ``Data`` array that are not vehicle
# telemetry, so they should not become sensors: account/message identifiers,
# report bookkeeping, and the bare unqualified fields (``value``/``state``/
# ``timestamp``) that lost their parent context in VW's flat record list.
_SKIP_FIELDS = {
    "vin",
    "user_id",
    "key",
    "message_id",
    "report_type",
    "update_reason",
    "error_code",
    "state",
    "value",
    "timestamp",
    # API health-check artifact (observed value is the literal string "echo"),
    # not vehicle telemetry.
    "echo",
}

# Some documented signals ship with the generic placeholder dataFieldName
# ``value`` (their real name lives only in VW's data dictionary, keyed by the
# stable UUID). Resolve those by key so they aren't dropped as envelope noise.
# Extend as more keys are confirmed from the data dictionary.
_GENERIC_FIELD_NAMES = {"value", "state"}
_KNOWN_DATA_KEYS = {
    # Data Dictionary V4.0 (Continuous Data): "Value of the primary range" (km).
    "0ca40e18-0564-3eda-bcc0-7aee9ef44f04": "primary_range",
}


# 32-bit "unset" sentinel VW sends for a field it has no real reading for
# (e.g. mileage on a car that hasn't reported it yet). Not a real distance -
# dropping it to None avoids showing a nonsense multi-billion-km odometer.
_UINT32_SENTINEL = 2**32 - 1


def _coerce(value: Any) -> Any:
    """Turn obviously-numeric string values into int/float so they can graph.

    Also unwraps the ``<n>s`` second-duration form VW uses (e.g. ``6900s``)
    and drops the 32-bit "unset" sentinel value (see ``_UINT32_SENTINEL``).
    """
    if isinstance(value, str):
        v = value.strip()
        if re.fullmatch(r"-?\d+", v):
            value = int(v)
        elif re.fullmatch(r"-?\d*\.\d+", v):
            value = float(v)
        elif re.fullmatch(r"\d+s", v):  # "6900s" -> 6900 (seconds)
            value = int(v[:-1])
    if isinstance(value, int) and not isinstance(value, bool) and value == _UINT32_SENTINEL:
        return None
    return value


def _extract_values(raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Turn an EU Data Act dataset into ``{dataFieldName: latest value}``, plus
    the newest ``timestampUtc`` seen across all records.

    The payload's ``Data`` array is a flat list of ``{key, dataFieldName, value,
    timestampUtc}`` records — typically several timestamped samples of the same
    field. We key by ``dataFieldName`` (which is stable across the 15-min
    deliveries) and keep the last value seen, so each signal maps to exactly
    one sensor that tracks its most recent reading. Inner docs that don't use
    this shape fall back to a generic flatten (no per-record timestamp then).

    ``timestampUtc`` is the moment the car reported that record - on flat
    (pre-ID.x) payloads it's the only capture-time signal available (there is
    no dedicated ``car_captured_time`` field), and every record in a delivery
    carries the same value, so the max across the batch is that delivery's
    capture time.
    """
    out: dict[str, Any] = {}
    latest_ts: str | None = None
    for doc in raw.values():
        records = (doc.get("Data") or doc.get("data")) if isinstance(doc, dict) else None
        if isinstance(records, list):
            for r in records:
                if not isinstance(r, dict):
                    continue
                ts = r.get("timestampUtc")
                if isinstance(ts, str) and (latest_ts is None or ts > latest_ts):
                    latest_ts = ts
                name = r.get("dataFieldName") or r.get("datafieldname")
                # Recover a documented signal whose placeholder name would
                # otherwise be skipped, by resolving its stable key UUID.
                if name in _GENERIC_FIELD_NAMES:
                    name = _KNOWN_DATA_KEYS.get(r.get("key"), name)
                # Drop non-telemetry fields: ``*.value_type`` constant tags
                # (VALUE_TYPE_PHYSICAL) and ``*_unit`` descriptors that just name
                # another field's unit (e.g. charge_rate_unit) — neither is a
                # useful sensor.
                if not name or name in _SKIP_FIELDS or name.endswith((".value_type", "_unit")):
                    continue
                out[name] = _coerce(r.get("value"))
        elif isinstance(doc, (dict, list)):
            out.update(flatten(doc))
    # flatten()'s generic fallback (used for docs that aren't shaped as the
    # expected records list) has no notion of _SKIP_FIELDS, so an envelope
    # field like "echo" can leak through under a dotted path (e.g.
    # "some.prefix.echo") instead of the bare name the per-record loop above
    # already filters. Catch that here, matched on the final path segment.
    for key in list(out):
        if key.rsplit(".", 1)[-1] in _SKIP_FIELDS:
            del out[key]
    return out, latest_ts


class EuDataActClient:
    """Minimal async client for the EU Data Act portal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        brand: str = DEFAULT_BRAND,
        country: str = "de",
        language: str = "en",
    ) -> None:
        if brand not in BRAND_CLIENT_IDS:
            raise ValueError(f"Unknown brand {brand!r}; valid: {list(BRAND_CLIENT_IDS)}")
        self._session = session
        self._email = email
        self._password = password
        self._brand = brand
        self._country = country
        self._language = language
        self._logged_in = False

    # -- auth ---------------------------------------------------------------

    def _state(self) -> str:
        return f"{self._country}__{self._language}__{self._brand}"

    def _authorize_url(self) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": BRAND_CLIENT_IDS[self._brand],
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": self._state(),
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    async def login(self) -> None:
        """Run the signin-service OIDC code flow; establishes the portal session.

        Transport failures (DNS/connection/timeout) are re-raised as
        EuDataActError so callers report "cannot connect" instead of "unknown".
        """
        try:
            await self._login_flow()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EuDataActError(f"could not reach VW Identity: {err}") from err

    async def _login_flow(self) -> None:
        hdrs = {"User-Agent": USER_AGENT}
        # 0. prime portal (sets AEM cookies the callback needs)
        try:
            await self._session.get(BASE_URL + "/", headers=hdrs)
        except aiohttp.ClientError as err:
            _LOGGER.debug("EU Data Act priming GET failed (ignored): %s", err)

        # 1. authorize -> signin-service login page
        async with self._session.get(self._authorize_url(), headers=hdrs) as resp:
            page = await resp.text()
            page_url = str(resp.url)
        if "signin-service" not in page_url:
            raise EuDataActAuthError(f"authorize did not reach signin-service (url={page_url})")

        # 2. POST email (identifier step)
        fields, action = _login_fields(page)
        if not fields.get("hmac") or not fields.get("_csrf"):
            raise EuDataActAuthError("could not parse signin form (missing hmac/_csrf)")
        fields["email"] = self._email
        async with self._session.post(
            urljoin(page_url, action or ""), data=fields, headers={**hdrs, "Referer": page_url}
        ) as resp:
            page2 = await resp.text()
            page2_url = str(resp.url)

        # 3. POST password (authenticate step)
        fields2, action2 = _login_fields(page2)
        if not fields2.get("hmac"):
            raise EuDataActAuthError(_login_error(page2) or "no password form (check email address)")
        fields2["email"] = self._email
        fields2["password"] = self._password
        target = urljoin(page2_url, action2) if action2 else page2_url.split("?", 1)[0]
        async with self._session.post(
            target, data=fields2, headers={**hdrs, "Referer": page2_url}
        ) as resp:
            landing = await resp.text()
            landing_url = str(resp.url)

        if "signin-service" in landing_url or "/error" in landing_url:
            reason, message = _diagnose_login_failure(landing_url, landing)
            raise EuDataActAuthError(message, reason=reason)
        if urlparse(landing_url).hostname != urlparse(BASE_URL).hostname:
            raise EuDataActAuthError(f"login did not land on portal (url={landing_url})")
        self._logged_in = True
        _LOGGER.debug("EU Data Act login OK")

    async def _ensure_login(self) -> None:
        if not self._logged_in:
            await self.login()

    # -- HTTP helpers -------------------------------------------------------

    async def _request(self, method: str, url: str, *, headers=None, _retried=False):
        await self._ensure_login()
        full = url if url.startswith("http") else BASE_URL + url
        h = {"User-Agent": USER_AGENT, **(headers or {})}
        try:
            async with self._session.request(method, full, headers=h) as resp:
                status = resp.status
                body = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EuDataActError(f"{method} {full} failed: {err}") from err
        # Auth failure OR Adobe-AEM session-expiry (5xx + HTML body) -> re-login once.
        looks_aem = status >= 500 and body[:1] == b"<"
        if (status in (401, 403) or looks_aem) and not _retried:
            _LOGGER.info("EU Data Act %s %s -> %s; re-login + retry", method, full, status)
            self._logged_in = False
            await self.login()
            return await self._request(method, url, headers=headers, _retried=True)
        return status, body

    async def _get_json(self, url: str, headers=None):
        status, body = await self._request("GET", url, headers=headers)
        text = body.decode("utf-8", "replace")
        if status >= 400:
            raise EuDataActError(f"GET {url} -> HTTP {status}: {text[:200]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise EuDataActError(f"invalid JSON from {url}: {text[:200]}") from err

    # -- API ----------------------------------------------------------------

    async def list_vehicles(self) -> list[dict]:
        """Return enrolled vehicles: [{vin, nickName, licensePlate, role, enrollmentStatus, …}]."""
        payload = await self._get_json(VEHICLES_PATH + "?viewPosition=FRONT_LEFT")
        return payload if isinstance(payload, list) else payload.get("vehicles", [])

    async def get_metadata(self, vin: str) -> dict:
        """Return the configured data-request package for a VIN.

        Raises EuDataActNotConfigured if the user has not enabled a data request.
        """
        try:
            meta = await self._get_json(METADATA_PATH.format(vin=vin))
        except EuDataActError as err:
            if "No data request" in str(err):
                raise EuDataActNotConfigured(
                    f"No continuous data request configured for {vin}. Enable one at {BASE_URL}."
                ) from err
            raise
        if isinstance(meta, list):
            meta = meta[0] if meta else {}
        return meta

    async def list_datasets(self, vin: str, identifier: str) -> list[dict]:
        data = await self._get_json(
            LIST_PATH.format(vin=vin, identifier=identifier), headers={"type": "partial"}
        )
        files = data.get("files") if isinstance(data, dict) else data
        return files if isinstance(files, list) else []

    async def download_dataset(self, vin: str, identifier: str, name: str) -> dict:
        """Download one dataset ZIP and return the parsed inner JSON (merged)."""
        if name.endswith(NO_CONTENT_SUFFIX):
            raise EuDataActError(f"{name} contains no content")
        status, body = await self._request(
            "GET",
            DOWNLOAD_PATH.format(vin=vin, identifier=identifier),
            headers={"filename": name, "type": "partial"},
        )
        if status >= 400:
            raise EuDataActError(f"download {name} -> HTTP {status}")
        merged: dict[str, Any] = {}
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for inner in zf.namelist():
                if not inner.endswith(".json"):
                    continue
                try:
                    merged[inner] = json.loads(zf.read(inner))
                except json.JSONDecodeError:
                    _LOGGER.debug("EU Data Act: %s not valid JSON, skipped", inner)
        return merged

    async def get_latest(self, vin: str, identifier: str | None = None) -> dict | None:
        """Convenience: newest content dataset for a VIN, parsed + flattened.

        Returns {identifier, dataset, created_on, raw, values, captured_at} or
        None when no content has been delivered yet (e.g. car idle / request
        just activated). ``captured_at`` is the newest per-record
        ``timestampUtc`` in the dataset (see _extract_values) - None if the
        payload didn't carry one.
        """
        if identifier is None:
            identifier = (await self.get_metadata(vin)).get("Identifier")
        if not identifier:
            return None
        datasets = await self.list_datasets(vin, identifier)
        content = [d for d in datasets if d.get("name") and not d["name"].endswith(NO_CONTENT_SUFFIX)]
        if not content:
            return None
        newest = max(content, key=lambda d: str(d.get("createdOn") or d.get("name")))
        raw = await self.download_dataset(vin, identifier, newest["name"])
        values, captured_at = _extract_values(raw)
        return {
            "identifier": identifier,
            "dataset": newest["name"],
            "created_on": newest.get("createdOn"),
            "raw": raw,
            "values": values,
            "captured_at": captured_at,
        }


def _diagnose_login_failure(url: str, body: str) -> tuple[str, str]:
    """Classify a signin-service failure into a (config-flow error key, message).

    The error key drives what the user is shown, so consent-not-accepted and
    throttling read as themselves instead of a misleading "invalid email or
    password" (see #14).
    """
    if "/consent" in url or "/terms-and-conditions" in url or "consent-screen" in (body or ""):
        return "not_authorised", (
            "EU Data Act portal not yet authorised for this account. Open "
            f"{BASE_URL}/ in a browser, log in, click Allow on the consent screen, "
            "and finish portal setup (vehicle linking + continuous 15-min data request)."
        )
    code = ""
    try:
        from urllib.parse import parse_qs

        code = parse_qs(urlparse(url).query).get("error", [""])[0]
    except (ValueError, KeyError):
        pass
    code = code or _login_error(body) or ""
    if re.search(r"password_invalid", code, re.I):
        return "invalid_auth", "Login failed: password incorrect."
    if re.search(r"email_invalid|user_id|identifier", code, re.I):
        return "invalid_auth", "Login failed: email not recognised by VW Identity."
    if re.search(r"throttle|rate_limit|too_many", code, re.I):
        return "throttled", "Login failed: too many attempts, throttled by VW. Wait ~30 min."
    if re.search(r"account_disabled|locked|blocked", code, re.I):
        return "account_locked", "Login failed: VW account locked/disabled."
    return "invalid_auth", (
        f"Login failed: {code}" if code else f"Login failed (no error code). URL: {url}"
    )
