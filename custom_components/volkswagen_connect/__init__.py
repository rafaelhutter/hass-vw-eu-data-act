"""The Volkswagen Connect integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .binary_sensor import BINARY_KEYS
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.CALENDAR,
]


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


async def async_setup_entry(hass: HomeAssistant, entry: VolkswagenConnectConfigEntry) -> bool:
    """Set up Volkswagen Connect from a config entry."""
    coordinator = VolkswagenConnectCoordinator(hass, entry)
    # Roll the website-portal session up front so the very first poll runs on a
    # fresh session (the restored cookies may be stale after a long downtime).
    await coordinator.async_refresh_session(force=True)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await _migrate_binary_keys(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VolkswagenConnectConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
