"""Async client for the volkswagen.de website portal (authproxy).

A second, more *reliable* data source alongside the EU Data Act portal. The
website `authproxy` is a server-side confidential OAuth client, so its data
(odometer / service-due / vehicle info / capabilities) is always available once
authenticated — no dependency on the car reporting into a 15-min slot.

Auth: the website client (`4fb52a96-…`) uses Auth0 universal login and triggers
**email-OTP MFA**, so login is two-phase:
    state = await client.begin_login()      # "ok" or "otp_required"
    if state == "otp_required":
        await client.submit_otp(code)        # code from the user's email
The resulting session (cookies) is exportable for persistence; while the `auth0`
SSO cookie is valid, `refresh()` re-establishes the short-lived portal session
silently (no credentials, no OTP). When it expires, refresh() raises
WebsitePortalAuthError -> the integration triggers HA reauth.

Caller must pass an aiohttp.ClientSession with its OWN cookie jar.

Header recipe per endpoint (discovered live): `x-csrf-token` = csrf_token cookie,
`user-id: __userId__` (authproxy substitutes it), and a per-endpoint `Accept`
version (maintenance/status wants `*/*`; usercapabilities wants
`application/json;version=3`).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from yarl import URL

_LOGGER = logging.getLogger(__name__)

PORTAL = "https://www.volkswagen.de"
AUTHPROXY_LOGIN = (
    PORTAL + "/app/authproxy/login?fag=vw-de,vwag-weconnect"
    "&scope-vw-de=profile,address,phone,carConfigurations,dealers,cars,vin,profession"
    "&scope-vwag-weconnect=openid,mbb&prompt-vwag-weconnect=none"
    "&redirectUrl=" + PORTAL + "/de/besitzer-und-nutzer/myvolkswagen.html"
    "&sessionTimeout=1800"
)
IDENTITY = "https://identity.vwgroup.io"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
REFERER = PORTAL + "/de/besitzer-und-nutzer/myvolkswagen.html"

# VW's silent SSO now chains two OAuth dances through Auth0 federation — well
# past aiohttp's default cap of 10, which surfaced as "refresh request failed: 0".
MAX_REDIRECTS = 30


# VW's 2026 "consent wall" interrupts the still-valid SSO with a consent/terms
# screen; auto-accepting legal terms is fragile, so we tell the user to clear it.
_CONSENT_HINT = (
    "Volkswagen is asking you to re-accept a consent/permissions screen. "
    "Open volkswagen.de in a browser, sign in, accept it, then reconfigure the "
    "Volkswagen Connect integration (a reinstall won't help)."
)


def _is_consent_url(url: str) -> bool:
    low = url.lower()
    return "/consent" in low or "/terms-and-conditions" in low


class WebsitePortalError(Exception):
    """Base error."""


class WebsitePortalAuthError(WebsitePortalError):
    """Login/refresh failed; full re-auth (incl. OTP) needed.

    ``reason`` optionally classifies *why*, using the same vocabulary as
    ``EuDataActAuthError.reason`` (see ``exceptions.classify``) - "" when the
    call site doesn't know more than "auth failed".
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class _MfaRequired(Exception):
    def __init__(self, page_url: str, html: str) -> None:
        self.page_url = page_url
        self.html = html


def _image_views(urls: Any) -> dict[str, str]:
    """Map VILMA image URLs to {view_key: url} by their path segment.

    e.g. ``.../2025/Side_Left/<hash>.png`` -> ``{"side_left": url}``.
    """
    out: dict[str, str] = {}
    for u in urls if isinstance(urls, list) else []:
        if not isinstance(u, str):
            continue
        m = re.search(r"/([A-Za-z]+_[A-Za-z]+)/[^/]+\.\w+$", u)
        if m:
            out[m.group(1).lower()] = u
    return out


def _parse_form(html: str) -> tuple[dict[str, str], str | None]:
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
    return fields, action


class WebsitePortalClient:
    """Website-session (authproxy) client."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._mfa: dict[str, Any] | None = None
        self._gdc_cache: dict[str, str] = {}

    # -- cookie persistence -------------------------------------------------

    # Hosts the session spans. Used as the broadcast fallback for legacy
    # (pre-0.5.9) persisted cookies that carry no domain.
    _HOSTS = ("https://www.volkswagen.de/", "https://identity.vwgroup.io/")

    def export_cookies(self) -> list[dict[str, str]]:
        """Persist cookies as (domain, key, value) triples.

        Keyed by (domain, key) — NOT key alone — so a cookie that exists on
        both www.volkswagen.de and identity.vwgroup.io (VW's stack reuses
        cookie names across hosts) no longer clobbers its twin. Collapsing on
        the bare key was corrupting the identity-host SSO cookie on every
        persist, which silently broke the silent refresh and forced a re-login.
        """
        seen: dict[tuple[str, str], str] = {}
        for cookie in self._session.cookie_jar:
            domain = cookie["domain"] or ""
            seen[(domain, cookie.key)] = cookie.value
        return [{"domain": d, "key": k, "value": v} for (d, k), v in seen.items()]

    def import_cookies(self, data: list[dict[str, str]]) -> None:
        """Restore cookies to their origin host.

        The 0.5.9+ format carries a per-cookie ``domain``; each cookie is filed
        back on its own host (a dotted domain like ``.vwgroup.io`` stays a
        domain cookie). Legacy entries without a domain are broadcast to both
        hosts — the old best-effort behaviour — so existing installs keep
        working until the next persist upgrades them in place.
        """
        legacy: SimpleCookie = SimpleCookie()
        for c in data:
            key = c.get("key")
            if not key:
                continue
            value = c.get("value", "")
            domain = (c.get("domain") or "").strip()
            if not domain:
                legacy[key] = value
                continue
            sc: SimpleCookie = SimpleCookie()
            sc[key] = value
            sc[key]["path"] = "/"
            if domain.startswith("."):
                sc[key]["domain"] = domain
            self._session.cookie_jar.update_cookies(
                sc, URL(f"https://{domain.lstrip('.')}/")
            )
        if legacy:
            for host in self._HOSTS:
                self._session.cookie_jar.update_cookies(legacy, URL(host))

    def _csrf(self) -> str | None:
        for cookie in self._session.cookie_jar:
            if cookie.key == "csrf_token":
                return cookie.value
        return None

    # -- login --------------------------------------------------------------

    async def _follow(self, start_url: str, max_hops: int = MAX_REDIRECTS) -> str:
        """Follow redirects to the portal; raise _MfaRequired on the OTP page."""
        ref = start_url
        for _ in range(max_hops):
            host = urlparse(ref).hostname or ""
            if host.endswith("volkswagen.de") and "/app/authproxy/login" not in ref:
                return ref
            async with self._session.get(
                ref, headers={"User-Agent": UA}, allow_redirects=False
            ) as r:
                status = r.status
                loc = r.headers.get("Location")
                cur = str(r.url)
                body = await r.text() if status == 200 else ""
            if "/u/mfa-email-challenge" in cur and status == 200:
                raise _MfaRequired(cur, body)
            if not loc:
                return cur
            ref = urljoin(ref, loc)
        raise WebsitePortalError("too many redirects during login")

    async def begin_login(self) -> str:
        """Start login. Returns 'ok' (logged in) or 'otp_required'."""
        async with self._session.get(
            AUTHPROXY_LOGIN,
            headers={"User-Agent": UA},
            allow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as r:
            page_url = str(r.url)
        if "/u/login" not in page_url:
            if _is_consent_url(page_url):
                raise WebsitePortalAuthError(_CONSENT_HINT, reason="not_authorised")
            if (urlparse(page_url).hostname or "").endswith("volkswagen.de"):
                return "ok"  # silent SSO
            raise WebsitePortalAuthError(f"unexpected authorize landing: {page_url}")
        state = parse_qs(urlparse(page_url).query).get("state", [None])[0]
        async with self._session.post(
            f"{IDENTITY}/u/login?state={state}",
            data={"state": state, "username": self._email, "password": self._password},
            headers={"User-Agent": UA},
            allow_redirects=False,
        ) as r:
            loc = r.headers.get("Location")
            cur = str(r.url)
        if not loc:
            raise WebsitePortalAuthError("login rejected (wrong credentials?)")
        try:
            await self._follow(urljoin(cur, loc))
            return "ok"
        except _MfaRequired as mfa:
            fields, action = _parse_form(mfa.html)
            self._mfa = {"page_url": mfa.page_url, "fields": fields, "action": action}
            return "otp_required"

    async def submit_otp(self, code: str) -> str:
        if not self._mfa:
            raise WebsitePortalError("no MFA challenge pending")
        fields = dict(self._mfa["fields"])
        for key in list(fields):
            if key.lower() in ("code", "otp", "mfa-code", "token"):
                fields[key] = code
            if "remember" in key.lower():
                fields[key] = "true"
        fields.setdefault("code", code)
        target = urljoin(self._mfa["page_url"], self._mfa["action"] or "")
        async with self._session.post(
            target, data=fields, headers={"User-Agent": UA}, allow_redirects=False
        ) as r:
            loc = r.headers.get("Location")
            cur = str(r.url)
        if not loc:
            raise WebsitePortalAuthError("OTP rejected")
        await self._follow(urljoin(cur, loc))
        self._mfa = None
        return "ok"

    async def refresh(self) -> None:
        """Silently re-establish the portal session via the SSO cookie."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            present = sorted(
                {(c["domain"] or "?", c.key) for c in self._session.cookie_jar}
            )
            _LOGGER.debug(
                "Portal refresh: %d cookies present %s", len(present), present
            )
        try:
            async with self._session.get(
                AUTHPROXY_LOGIN,
                headers={"User-Agent": UA},
                allow_redirects=True,
                max_redirects=MAX_REDIRECTS,
            ) as r:
                url = str(r.url)
                status = r.status
                body = await r.text()
        except aiohttp.TooManyRedirects as err:
            # A genuine loop (not just a long chain). A consent hop in the
            # history means the consent wall is bouncing us — tell the user.
            chain = [str(r.url) for r in err.history]
            if any(_is_consent_url(u) for u in chain):
                raise WebsitePortalAuthError(_CONSENT_HINT, reason="not_authorised") from err
            raise WebsitePortalError(
                f"redirect loop during silent refresh ({len(chain)} hops, "
                f"last: {chain[-1] if chain else '?'})"
            ) from err
        except aiohttp.ClientError as err:
            raise WebsitePortalError(f"refresh request failed: {err}") from err
        # Consent/terms wall: checked before the generic re-auth hints so its
        # actionable message wins (old-style consent also matches /signin-service).
        if _is_consent_url(url):
            raise WebsitePortalAuthError(_CONSENT_HINT, reason="not_authorised")
        if "/u/login" in url or "/signin-service" in url:
            raise WebsitePortalAuthError("SSO session expired; full re-auth required")
        # A failed silent auth (prompt=none) can still bounce back to the portal
        # redirectUrl carrying ?error=login_required|consent_required, so the
        # host check alone reads it as success. Treat any error param as an
        # expired SSO instead of caching a dead session and reporting "ok".
        sso_error = parse_qs(urlparse(url).query).get("error", [None])[0]
        if sso_error:
            raise WebsitePortalAuthError(
                f"SSO silent refresh failed (error={sso_error}); re-auth required"
            )
        if not (urlparse(url).hostname or "").endswith("volkswagen.de"):
            raise WebsitePortalAuthError(f"refresh did not land on portal: {url}")
        # Landing on a volkswagen.de URL is necessary but not sufficient: the
        # chain can also terminate on a non-2xx error response (e.g. 412) that
        # still resolves to a volkswagen.de host, which every check above would
        # wave through as "ok" while the session is actually still dead.
        if status != 200:
            _LOGGER.debug("Portal refresh landed with HTTP %d: %s", status, body[:300])
            raise WebsitePortalAuthError(
                f"refresh landed on portal but HTTP {status}; re-auth required"
            )

    # -- data ---------------------------------------------------------------

    async def _get(
        self, path: str, accept: str = "application/json", _retried: bool = False
    ) -> tuple[int, str]:
        headers = {
            "User-Agent": UA,
            "Accept": accept,
            # Some endpoints (e.g. warninglights/last) 400 without a language;
            # the website always sends de-DE. Harmless for the others.
            "Accept-Language": "de-DE",
            "x-csrf-token": self._csrf() or "",
            "user-id": "__userId__",
            "traceId": uuid.uuid4().hex,
            "Referer": REFERER,
        }
        async with self._session.get(
            PORTAL + path, headers=headers, allow_redirects=False
        ) as r:
            status = r.status
            location = r.headers.get("Location", "")
            body = await r.text()
        # Session expired -> re-auth once via the SSO cookie and retry. Detected
        # as 401/403/412/428, a redirect to the login/authorize pages, or an AEM
        # HTML error (5xx + '<'). The authproxy answers a *stale* session with
        # 412 Precondition Failed (not 401), so without it here every endpoint
        # silently returned {} and the user saw no entities. refresh() raises
        # WebsitePortalAuthError if the SSO itself is gone, which the caller turns
        # into a "reconfigure" notice.
        login_redirect = status in (301, 302, 303, 307, 308) and any(
            s in location for s in ("/u/login", "/signin-service", "/authorize")
        )
        auth_failed = status in (401, 403, 412, 428)
        if not _retried and (auth_failed or login_redirect or (status >= 500 and body[:1] == "<")):
            await self.refresh()
            return await self._get(path, accept, _retried=True)
        # Still rejected after a fresh session -> the session is genuinely dead.
        # Surface it so the coordinator prompts a reconfigure instead of quietly
        # returning empty data for every endpoint.
        if _retried and (auth_failed or login_redirect):
            # VW's body names the failing precondition (e.g. consent) — see #20.
            _LOGGER.debug(
                "portal %s -> %s after refresh; body: %.300s", path, status, body
            )
            raise WebsitePortalAuthError(
                f"portal session rejected (HTTP {status}) after refresh; re-auth required"
            )
        return status, body

    async def get_first_vin(self) -> str | None:
        status, body = await self._get(
            "/app/authproxy/vw-de/proxy/v2/users/me/relations?resourceHost=myvw-vum-prod"
        )
        if status == 401 or status == 403:
            raise WebsitePortalAuthError("unauthorized")
        m = re.findall(r'"vin"\s*:\s*"([A-Z0-9]{17})"', body)
        return m[0] if m else None

    async def _gdc_for(self, vin: str) -> str:
        """Resolve the authproxy backend cluster ('gdc') this vehicle is routed to.

        VW's own frontend picks this per vehicle rather than using one fixed
        value - a hardcoded "myvw-wcar-prod" 412s (Precondition Failed) for
        vehicles routed elsewhere, regardless of session validity. The
        relation record names the real backend directly as
        relation.vehicle.modBackend (e.g. "MBB_ODP" for a combustion Tiguan);
        take the prefix before the underscore, lowercased, as the gdc value.
        Cached per VIN - this doesn't change for the life of a session. Falls
        back to "wcar" (what every release up to 0.5.26 hardcoded, so a hiccup
        here can't break the vehicles that already worked) if the field is
        missing or the shape changes, rather than blocking every data call.
        """
        if vin in self._gdc_cache:
            return self._gdc_cache[vin]
        gdc = "wcar"
        try:
            status, body = await self._get(
                f"/app/authproxy/vw-de/proxy/v2/users/me/relations/{vin}"
                "?resourceHost=myvw-vum-prod"
            )
            if status == 200:
                mod_backend = (
                    json.loads(body).get("relation", {}).get("vehicle", {}).get("modBackend")
                    or ""
                )
                prefix = mod_backend.split("_", 1)[0]
                if prefix:
                    gdc = prefix.lower()
        except (ValueError, AttributeError, WebsitePortalAuthError) as err:
            _LOGGER.debug("Could not resolve gdc for %s, defaulting to %r: %s", vin, gdc, err)
        # Names the cluster a 412 would be coming from — first thing to ask for.
        _LOGGER.debug("Resolved gdc for %s: %s", vin, gdc)
        self._gdc_cache[vin] = gdc
        return gdc

    async def get_maintenance(self, vin: str) -> dict[str, Any]:
        """Returns mileage_km, inspectionDue_days/km, oilServiceDue_*, carCapturedTimestamp."""
        gdc = await self._gdc_for(vin)
        status, body = await self._get(
            f"/app/authproxy/vw-de/proxy/vehicles/{vin}/maintenance/status"
            f"?gdc=myvw-{gdc}-prod&resourceHost=myvw-vcf-prod",
            accept="*/*",
        )
        if status != 200:
            _LOGGER.debug("portal maintenance %s -> %s", vin, status)
            return {}
        try:
            return json.loads(body).get("data", {})
        except ValueError:
            return {}

    async def get_charging(self, vin: str) -> dict[str, Any]:
        """Live EV status from charging/status -> flat dict of present values.

        Keys: soc, electric_range, target_soc, battery_temp (°C), charging_state,
        charge_power, charge_rate, charge_time_remaining, charge_mode,
        plug_connection, plug_lock, external_power.
        """
        gdc = await self._gdc_for(vin)
        status, body = await self._get(
            f"/app/authproxy/vwag-weconnect/proxy/vehicles/{vin}/charging/status"
            f"?gdc=myvw-{gdc}-prod&resourceHost=myvw-vcf-prod",
            accept="*/*",
        )
        if status != 200:
            _LOGGER.debug("portal charging %s -> %s", vin, status)
            return {}
        try:
            d = json.loads(body).get("data", {})
        except ValueError:
            return {}
        bs = d.get("batteryStatus") or {}
        cs = d.get("chargingStatus") or {}
        ps = d.get("plugStatus") or {}
        out: dict[str, Any] = {}

        def put(key: str, val: Any) -> None:
            if val is not None:
                out[key] = val

        put("soc", bs.get("currentSOC_pct"))
        put("electric_range", bs.get("cruisingRangeElectric_km"))
        put("target_soc", bs.get("navigationTargetSOC_pct"))
        temp_k = bs.get("temperatureHvBattery_K")
        if temp_k is not None:
            out["battery_temp"] = round(temp_k - 273.15, 1)
        put("charging_state", cs.get("chargingState"))
        put("charge_power", cs.get("chargePower_kW"))
        put("charge_rate", cs.get("chargeRate_kmph"))
        put("charge_time_remaining", cs.get("remainingChargingTimeToComplete_min"))
        put("charge_mode", cs.get("chargeMode"))
        put("plug_connection", ps.get("plugConnectionState"))
        put("plug_lock", ps.get("plugLockState"))
        put("external_power", ps.get("externalPower"))
        return out

    async def get_warning_lights(self, vin: str) -> dict[str, Any]:
        """Vehicle-health warning lights -> {"warning_lights": <count>}.

        warninglights/last returns the currently-active dashboard warning lights;
        an empty list means everything is OK (count 0).
        """
        gdc = await self._gdc_for(vin)
        status, body = await self._get(
            f"/app/authproxy/vwag-weconnect/proxy/vehicles/{vin}/warninglights/last"
            f"?gdc=myvw-{gdc}-prod&resourceHost=myvw-vcf-prod",
            accept="*/*",
        )
        if status != 200:
            _LOGGER.debug("portal warninglights %s -> %s", vin, status)
            return {}
        try:
            lights = json.loads(body).get("data", {}).get("warningLights")
        except ValueError:
            return {}
        if not isinstance(lights, list):
            return {}
        return {"warning_lights": len(lights)}

    async def get_lock_history(self, vin: str) -> dict[str, Any]:
        """Last *confirmed* remote lock/unlock command from the transaction log.

        This is NOT a live lock sensor: the live access/door/window status sits
        behind VW's secured-operations tier (only the attestation-backed mobile
        app can read it). The website exposes the lock/unlock *command history*
        instead, so we surface the most recent successful one as the best
        available signal. Keys: last_lock_action ("lock"/"unlock"),
        last_lock_action_time (ISO timestamp).
        """
        gdc = await self._gdc_for(vin)
        status, body = await self._get(
            f"/app/authproxy/vw-de/proxy/vehicles/{vin}/transactionhistory"
            f"?gdc=myvw-{gdc}-prod&resourceHost=myvw-vcf-prod",
            accept="*/*",
        )
        if status != 200:
            _LOGGER.debug("portal transactionhistory %s -> %s", vin, status)
            return {}
        try:
            messages = json.loads(body).get("data", {}).get("messages", [])
        except ValueError:
            return {}
        locks = [
            m
            for m in messages
            if isinstance(m, dict)
            and m.get("category") == "remotelockunlock"
            and m.get("action") in ("lock", "unlock")
        ]
        if not locks:
            return {}
        locks.sort(key=lambda m: m.get("timestamp", ""))
        # Prefer the most recent *successful* command; fall back to the latest.
        confirmed = [m for m in locks if m.get("actionSuccess")]
        chosen = (confirmed or locks)[-1]
        out: dict[str, Any] = {"last_lock_action": chosen["action"]}
        if chosen.get("timestamp"):
            out["last_lock_action_time"] = chosen["timestamp"]
        return out

    async def get_vehicle_images(self, vin: str) -> dict[str, str]:
        """All exterior view URLs from VILMA, keyed by view (e.g. 'side_left').

        A single call returns the whole set (side/front/back x left/center/right);
        the viewDirection/angle params only reorder it. Public CDN, no auth.
        """
        status, body = await self._get(
            f"/app/authproxy/vw-de/proxy/vehicleimages/exterior/{vin}"
            "?viewDirection=side&angle=left&resourceHost=myvw-vilma-proxy-prod",
            accept="*/*",
        )
        if status != 200:
            _LOGGER.debug("portal vehicleimages %s -> %s", vin, status)
            return {}
        try:
            urls = json.loads(body).get("imageUrls")
        except ValueError:
            return {}
        return _image_views(urls)

    async def get_vehicle_info(self, vin: str) -> dict[str, Any]:
        """Static vehicle info: modelName, exteriorColor, engine, nickname, licensePlate."""
        info: dict[str, Any] = {}
        st, body = await self._get(
            f"/app/authproxy/vw-de/proxy/vehicles/{vin}/data/de-DE"
            "?resourceHost=cwat-group-vehicle-file-service-prod"
        )
        if st == 200:
            try:
                info.update(json.loads(body))
            except ValueError:
                pass
        st, body = await self._get(
            f"/app/authproxy/vw-de/proxy/vehicles/{vin}/details/de-DE"
            "?resourceHost=cwat-group-vehicle-file-service-prod"
        )
        if st == 200:
            try:
                d = json.loads(body)
                if d.get("engine"):
                    info["engine"] = d["engine"]
            except ValueError:
                pass
        st, body = await self._get(
            f"/app/authproxy/vw-de/proxy/v2/users/me/relations/{vin}?resourceHost=myvw-vum-prod"
        )
        if st == 200:
            try:
                rel = json.loads(body).get("relation", {})
                if rel.get("vehicleNickname"):
                    info["nickname"] = rel["vehicleNickname"]
                if rel.get("licensePlate"):
                    info["licensePlate"] = rel["licensePlate"]
            except ValueError:
                pass
        return info
