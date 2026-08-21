"""Button platform: create the EU Data Act continuous data request.

Today, enabling the 15-min EU Data Act feed requires the user to open the
portal in a browser, sign in, link the vehicle and turn on a continuous
data request by hand (see the config flow's setup description). This
button calls the same "create data request" endpoint directly from HA for
any vehicle whose data status is STATUS_NOT_CONFIGURED, removing that
manual step.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STATUS_NOT_CONFIGURED
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator, VehicleData
from .eu_data_act import EuDataActAuthError

_LOGGER = logging.getLogger(__name__)


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
            if vin not in known and vehicle.status == STATUS_NOT_CONFIGURED
        ]
        known.update(due)
        if due:
            async_add_entities(
                VolkswagenConnectDataRequestButton(coordinator, vin) for vin in due
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


class VolkswagenConnectDataRequestButton(
    CoordinatorEntity[VolkswagenConnectCoordinator], ButtonEntity
):
    """Create the EU Data Act continuous data request for this vehicle."""

    _attr_has_entity_name = True
    _attr_name = "Enable EU Data Act request"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:database-plus"

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{vin}_data_request_button"

    @property
    def device_info(self) -> DeviceInfo | None:
        vehicle = (self.coordinator.data or {}).get(self._vin)
        return _device(vehicle) if vehicle else None

    async def async_press(self) -> None:
        # A dead session here is not this button's job to fix - the next
        # regular poll cycle will hit the same EuDataActAuthError and raise
        # the usual Repairs issue/reauth flow. Just report and stop.
        try:
            created = await self.coordinator.client.create_data_request(self._vin)
        except EuDataActAuthError as err:
            _LOGGER.warning(
                "Could not create an EU Data Act request for %s: %s", self._vin, err
            )
            return
        if not created:
            _LOGGER.warning(
                "Could not create an EU Data Act request for %s; check the "
                "debug log for the portal's response", self._vin,
            )
        await self.coordinator.async_request_refresh()
