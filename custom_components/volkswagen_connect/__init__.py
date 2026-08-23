"""The Volkswagen Connect integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .binary_sensor import BINARY_KEYS
from .const import DOMAIN
from .coordinator import (
    _PORTAL_DUPLICATES,
    VolkswagenConnectConfigEntry,
    VolkswagenConnectCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.IMAGE]


async def _migrate_binary_keys(
    hass: HomeAssistant, entry: VolkswagenConnectConfigEntry
) -> None:
    """Replace old lock/open text sensors (now binary sensors) and purge their history.

    unique_id is ``{vin}_{key}`` (VIN is 17 chars). The recorder has no
    purge-on-remove hook, so we also drop the removed entities' history rather
    than leaving it to age out over purge_keep_days. See README (Entities).
    """
    reg = er.async_get(hass)
    removed = []
    for e in er.async_entries_for_config_entry(reg, entry.entry_id):
        if e.domain == "sensor" and len(e.unique_id) > 18 and e.unique_id[18:] in BINARY_KEYS:
            reg.async_remove(e.entity_id)
            removed.append(e.entity_id)
    if not removed:
        return
    _LOGGER.info("Replaced legacy lock/open text sensors with binary sensors: %s", removed)
    if hass.services.has_service("recorder", "purge_entities"):
        await hass.services.async_call(
            "recorder",
            "purge_entities",
            {"entity_id": removed, "keep_days": 0},
            blocking=False,
        )


def _purge_portal_duplicates(
    hass: HomeAssistant, coordinator: VolkswagenConnectCoordinator
) -> None:
    """Remove EU Data Act sensors the portal has taken over (#18).

    Their raw fields stop being fed once the cleaner portal signal arrives, so
    entities registered before that left-over sat 'unavailable' forever. Only
    dropped where the portal actually delivered for that vehicle.
    """
    reg = er.async_get(hass)
    removed = []
    for vin, data in (coordinator.data or {}).items():
        if not data.portal_ok:
            continue
        for portal_key, eu_fields in _PORTAL_DUPLICATES.items():
            if data.values.get(portal_key) is None:
                continue
            for field in eu_fields:
                entity_id = reg.async_get_entity_id("sensor", DOMAIN, f"{vin}_{field}")
                if entity_id and field not in data.values:
                    reg.async_remove(entity_id)
                    removed.append(entity_id)
    if removed:
        _LOGGER.info("Removed EU Data Act sensors superseded by portal data: %s", removed)


async def async_setup_entry(hass: HomeAssistant, entry: VolkswagenConnectConfigEntry) -> bool:
    """Set up Volkswagen Connect from a config entry."""
    coordinator = VolkswagenConnectCoordinator(hass, entry)
    # Roll the website-portal session up front so the very first poll runs on a
    # fresh session (the restored cookies may be stale after a long downtime).
    await coordinator.async_refresh_session(force=True)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await _migrate_binary_keys(hass, entry)
    _purge_portal_duplicates(hass, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VolkswagenConnectConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
