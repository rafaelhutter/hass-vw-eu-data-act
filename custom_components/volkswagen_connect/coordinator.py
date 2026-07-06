"""DataUpdateCoordinator for the Volkswagen Connect integration.

Two data sources, merged per vehicle:
  * volkswagen.de authproxy — the reliable source (battery/charging, odometer,
    service, warning lights, lock history, image).
  * EU Data Act portal (optional, flaky) — 15-min "continuous data".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BRAND,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_WEBSITE_COOKIES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STATUS_NO_DATA,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
)
from .eu_data_act import (
    DEFAULT_BRAND,
    EuDataActAuthError,
    EuDataActClient,
    EuDataActError,
    EuDataActNotConfigured,
)
from .website_portal import WebsitePortalAuthError, WebsitePortalClient

_LOGGER = logging.getLogger(__name__)

# Don't roll the session more often than this (avoids a double refresh when the
# explicit startup refresh is immediately followed by the first poll).
_MIN_REFRESH_INTERVAL_S = 600


def _choose_primary_view(views: dict[str, str]) -> str | None:
    """Pick the hero view for the main Image entity: a side/profile shot.

    Not every model exposes 'side_left' (the ID.3 keys its profile differently),
    so fall back through side_right, any side view, then the first available.
    """
    for key in ("side_left", "side_right"):
        if key in views:
            return key
    side = next((k for k in views if k.startswith("side")), None)
    return side or next(iter(views), None)


@dataclass
class VehicleData:
    """Per-vehicle snapshot exposed to entities."""

    vin: str
    info: dict[str, Any]
    status: str = STATUS_NO_DATA
    identifier: str | None = None
    dataset: str | None = None
    created_on: str | None = None
    captured_at: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    image_url: str | None = None
    image_urls: dict[str, str] = field(default_factory=dict)
    primary_image_view: str | None = None
    portal_ok: bool = False


type VolkswagenConnectConfigEntry = ConfigEntry["VolkswagenConnectCoordinator"]

# maintenance/status field -> clean sensor key
_MAINTENANCE_MAP = {
    "mileage_km": "odometer",
    "inspectionDue_days": "inspection_due_days",
    "inspectionDue_km": "inspection_due_km",
    "oilServiceDue_days": "oil_service_due_days",
    "oilServiceDue_km": "oil_service_due_km",
    "carCapturedTimestamp": "last_report",
}

# portal sensor key -> EU Data Act dataFieldName(s) that report the same signal.
# When the portal (cleaner, with units/device-classes) delivers the value, the
# raw EU Data Act duplicate is dropped. Only applied when the portal value is
# actually present, so nothing disappears if the portal is unavailable.
_PORTAL_DUPLICATES = {
    "odometer": ("mileage.value",),
    "soc": ("battery_level_HV.value", "battery_state_report.soc"),
    "charge_power": ("battery_state_report.charge_power",),
    "charge_rate": ("battery_state_report.charge_rate",),
    "charging_state": ("charging_state_report.current_charge_state",),
    "charge_mode": ("charging_state_report.charge_mode",),
    "charge_time_remaining": ("battery_state_report.remaining_charging_time_complete",),
}

# EU Data Act fields carrying the moment the car *captured* the data (as opposed
# to when the portal delivered it, which is always fresh). Newest one wins.
_CAPTURE_FIELDS = (
    "car_captured_time",
    "instrument_cluster_time",
    "profile_state_report.car_captured_time",
    "profile_state_report.instrument_cluster_time",
    "car_captured_utc_timestamp",
)


def _best_captured_at(values: dict[str, Any]) -> str | None:
    """Most recent 'captured by the car' timestamp in an EU Data Act dataset.

    The dataset's *delivery* time is always current, but the underlying capture
    can lag hours while the car is parked. Surfacing the newest capture time lets
    the integration (and the user) tell fresh data from a re-delivered snapshot.
    """
    best = None
    for key in _CAPTURE_FIELDS:
        parsed = dt_util.parse_datetime(str(values.get(key) or ""))
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return best.isoformat() if best else None


class VolkswagenConnectCoordinator(DataUpdateCoordinator[dict[str, VehicleData]]):
    """Polls the website authproxy (and optionally the EU Data Act portal)."""

    def __init__(self, hass: HomeAssistant, entry: VolkswagenConnectConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=DEFAULT_SCAN_INTERVAL)
        self.entry = entry
        # Monotonic timestamp of the last portal session refresh (None = never).
        self._last_refresh: float | None = None
        self.client = EuDataActClient(
            async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar()),
            email=entry.data[CONF_EMAIL],
            password=entry.data[CONF_PASSWORD],
            brand=entry.data.get(CONF_BRAND, DEFAULT_BRAND),
        )
        # Website portal is optional: only active if we have a persisted session.
        self.portal: WebsitePortalClient | None = None
        cookies = entry.data.get(CONF_WEBSITE_COOKIES)
        if cookies:
            self.portal = WebsitePortalClient(
                async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar()),
                email=entry.data[CONF_EMAIL],
                password=entry.data[CONF_PASSWORD],
            )
            self.portal.import_cookies(cookies)

    async def _async_update_data(self) -> dict[str, VehicleData]:
        result: dict[str, VehicleData] = {}
        try:
            vehicles = await self.client.list_vehicles()
        except EuDataActAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EuDataActError as err:
            # The EU Data Act backend is flaky; with a portal session available
            # its outage must not take down the whole integration.
            if self.portal is None:
                raise UpdateFailed(str(err)) from err
            _LOGGER.warning(
                "EU Data Act vehicle list failed (%s); continuing with portal data only", err
            )
            # Carry known vehicles forward (EU Data Act values stay empty this
            # cycle) so the portal merge below still refreshes its half.
            result = {
                vin: VehicleData(vin=vin, info=dict(d.info))
                for vin, d in (self.data or {}).items()
            }
            vehicles = []
        for v in vehicles:
            vin = v.get("vin")
            if not vin:
                continue
            data = VehicleData(vin=vin, info=dict(v))
            try:
                meta = await self.client.get_metadata(vin)
                data.identifier = meta.get("Identifier")
                latest = await self.client.get_latest(vin, data.identifier)
                if latest is None:
                    data.status = STATUS_NO_DATA
                else:
                    data.status = STATUS_OK
                    data.dataset = latest["dataset"]
                    data.created_on = latest["created_on"]
                    data.values = dict(latest["values"])
                    data.captured_at = _best_captured_at(data.values)
            except EuDataActNotConfigured:
                data.status = STATUS_NOT_CONFIGURED
            except EuDataActAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except EuDataActError as err:
                _LOGGER.warning("EU Data Act: %s update failed: %s", vin, err)
            result[vin] = data

        if self.portal is not None:
            await self._merge_portal(result)
        return result

    def _persist_portal_cookies(self) -> None:
        """Save the current portal cookies so the session survives a restart."""
        assert self.portal is not None
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_WEBSITE_COOKIES: self.portal.export_cookies()},
        )

    async def async_refresh_session(self, *, force: bool = False) -> None:
        """Roll the website-portal session (best-effort) and persist it.

        Called once at startup and again at the start of every poll, so both the
        portal's downstream tokens and the identity SSO behind the silent refresh
        stay rolled. Skips if a refresh happened within the last few minutes
        (unless ``force``), to avoid a double refresh when the startup call is
        immediately followed by the first poll. Never raises.
        """
        if self.portal is None:
            return
        if (
            not force
            and self._last_refresh is not None
            and time.monotonic() - self._last_refresh < _MIN_REFRESH_INTERVAL_S
        ):
            return
        try:
            await self.portal.refresh()
            self._last_refresh = time.monotonic()
            self._persist_portal_cookies()
        except WebsitePortalAuthError as err:
            _LOGGER.debug(
                "Session refresh: SSO not usable yet (%s); trying existing session",
                err,
            )
        except Exception as err:  # noqa: BLE001 - never block on a refresh hiccup
            _LOGGER.debug("Session refresh failed (continuing): %s", err)

    async def _merge_portal(self, result: dict[str, VehicleData]) -> None:
        """Best-effort: enrich vehicles with portal data. Never blocks setup.

        Keep-alive: the portal session's downstream tokens expire ~30 min after
        the last login and are NOT renewed by data calls, while the identity SSO
        behind the silent refresh lapses if it isn't exercised often enough. So
        we proactively roll the session every cycle (15 min, well inside that
        window) instead of only reacting to a 401 — by which point the SSO has
        often already gone, forcing a needless re-login. Best-effort: if the
        refresh fails we still try the existing session, and only a failing
        *data* call is treated as a real auth loss.
        """
        assert self.portal is not None
        await self.async_refresh_session()

        session_dead = False
        try:
            # If EU Data Act surfaced no vehicles, discover the VIN via the portal.
            if not result:
                vin = await self.portal.get_first_vin()
                if vin:
                    result[vin] = VehicleData(vin=vin, info={"vin": vin})
            for vin, data in result.items():
                maint = await self.portal.get_maintenance(vin)
                for raw, clean in _MAINTENANCE_MAP.items():
                    if maint.get(raw) is not None:
                        data.values[clean] = maint[raw]
                # Live battery/charging telemetry (already clean keys).
                data.values.update(await self.portal.get_charging(vin))
                # Vehicle-health warning lights + last lock/unlock command.
                data.values.update(await self.portal.get_warning_lights(vin))
                data.values.update(await self.portal.get_lock_history(vin))
                # Exterior images (public CDN URLs, served by the image platform).
                # All views in one call; a side/profile shot is the primary "Image".
                data.image_urls = await self.portal.get_vehicle_images(vin)
                data.primary_image_view = _choose_primary_view(data.image_urls)
                data.image_url = data.image_urls.get(data.primary_image_view or "")
                info = await self.portal.get_vehicle_info(vin)
                for k in ("nickName", "nickname", "licensePlate", "modelName", "engine", "exteriorColor"):
                    if info.get(k) and not data.info.get(k):
                        data.info[k] = info[k]
                # Drop EU Data Act fields that duplicate a portal signal we got.
                for portal_key, eu_fields in _PORTAL_DUPLICATES.items():
                    if data.values.get(portal_key) is not None:
                        for field in eu_fields:
                            data.values.pop(field, None)
                data.portal_ok = True
        except WebsitePortalAuthError as err:
            session_dead = True
            _LOGGER.warning(
                "Website portal session expired (%s). Live data is paused; "
                "open the integration and Reconfigure to restore it.",
                err,
            )
        except Exception as err:  # noqa: BLE001 - portal must never break EU Data Act
            _LOGGER.warning("Website portal update failed, skipping this cycle: %s", err)
        finally:
            # Persist the freshest cookies so the rolled SSO survives a restart —
            # but only while the session is still alive. Saving a known-dead set
            # over a good one just guarantees the next start also fails.
            if not session_dead:
                try:
                    self._persist_portal_cookies()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Could not persist portal cookies: %s", err)
