"""Number platform: a single account-level poll-interval slider.

One entity per config entry (not per vehicle) - it controls the shared
coordinator's polling cadence. Changing it takes effect immediately (no
integration reload) and is persisted to the config entry's options so it
survives a restart.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.number import NumberEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VolkswagenConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([VolkswagenConnectScanIntervalNumber(entry)])


class VolkswagenConnectScanIntervalNumber(NumberEntity):
    """How often (in minutes) the coordinator polls Volkswagen."""

    _attr_has_entity_name = True
    _attr_name = "Poll interval"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-cog-outline"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = MIN_SCAN_INTERVAL_MINUTES
    _attr_native_max_value = MAX_SCAN_INTERVAL_MINUTES
    _attr_native_step = 5

    def __init__(self, entry: VolkswagenConnectConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_scan_interval"
        default_minutes = int(DEFAULT_SCAN_INTERVAL.total_seconds() / 60)
        self._attr_native_value = entry.options.get(CONF_SCAN_INTERVAL, default_minutes)
        # Not tied to any one vehicle - it controls the shared coordinator - so
        # it gets its own lightweight service-level device instead of being an
        # orphan entity with no device page.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Volkswagen Connect",
            manufacturer="Volkswagen",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(value)
        coordinator: VolkswagenConnectCoordinator = self._entry.runtime_data
        coordinator.update_interval = timedelta(minutes=minutes)
        self._attr_native_value = minutes
        self.async_write_ha_state()
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_SCAN_INTERVAL: minutes}
        )
