"""Device tracker platform: last-known GPS position, where available.

EXPERIMENTAL: website_portal.get_parking_position hits a proxy path VW's own
myvolkswagen.de site never uses (it has no "find my car" map), so most
accounts will simply never populate latitude/longitude - this platform then
never spawns an entity for that vehicle, which is the correct, silent
degrade (see coordinator._merge_portal).
"""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator, VehicleData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VolkswagenConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        due = [
            vin
            for vin, vehicle in (coordinator.data or {}).items()
            if vin not in known and vehicle.latitude is not None
        ]
        known.update(due)
        if due:
            async_add_entities(
                VolkswagenConnectTracker(coordinator, vin) for vin in due
            )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def _device(vehicle: VehicleData) -> DeviceInfo:
    name = vehicle.info.get("nickName") or vehicle.info.get("licensePlate") or vehicle.vin
    return DeviceInfo(
        identifiers={(DOMAIN, vehicle.vin)},
        manufacturer="Volkswagen",
        name=name,
        model=vehicle.info.get("nickName"),
        serial_number=vehicle.vin,
    )


class VolkswagenConnectTracker(CoordinatorEntity[VolkswagenConnectCoordinator], TrackerEntity):
    """Last-known parked position for one vehicle."""

    _attr_has_entity_name = True
    _attr_translation_key = "location"
    _attr_icon = "mdi:map-marker"
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{vin}_location"

    @property
    def _vehicle(self) -> VehicleData | None:
        return (self.coordinator.data or {}).get(self._vin)

    @property
    def device_info(self) -> DeviceInfo | None:
        v = self._vehicle
        return _device(v) if v else None

    @property
    def latitude(self) -> float | None:
        v = self._vehicle
        return v.latitude if v else None

    @property
    def longitude(self) -> float | None:
        v = self._vehicle
        return v.longitude if v else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        v = self._vehicle
        if not v or not v.position_captured_at:
            return None
        return {"position_captured_at": v.position_captured_at}
