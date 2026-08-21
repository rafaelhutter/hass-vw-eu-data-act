"""Image platform: the vehicle's exterior photos.

The authproxy resolves a VIN to a set of public VILMA CDN URLs (side/front/back
x left/center/right). A side/profile view (chosen per vehicle) is the primary
"Image" entity; the other views are added disabled-by-default so they don't
clutter the UI. The CDN assets need no authentication, so entities are served by
URL.
"""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator, VehicleData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VolkswagenConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    # Track (vin, view) pairs; view=None is the primary side-view "Image".
    known: set[tuple[str, str | None]] = set()

    @callback
    def _add_new() -> None:
        new: list[ImageEntity] = []
        for vin, vehicle in (coordinator.data or {}).items():
            if vehicle.image_url and (vin, None) not in known:
                known.add((vin, None))
                new.append(VolkswagenConnectVehicleImage(hass, coordinator, vin))
            for view in vehicle.image_urls:
                if view == vehicle.primary_image_view or (vin, view) in known:
                    continue
                known.add((vin, view))
                new.append(VolkswagenConnectVehicleImage(hass, coordinator, vin, view))
        if new:
            async_add_entities(new)

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


class VolkswagenConnectVehicleImage(CoordinatorEntity[VolkswagenConnectCoordinator], ImageEntity):
    """An exterior photo of the vehicle: the primary side view, or a named view."""

    _attr_has_entity_name = True
    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: VolkswagenConnectCoordinator,
        vin: str,
        view: str | None = None,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._vin = vin
        self._view = view  # None = primary side-left view (legacy "Image" entity)
        if view is None:
            self._attr_unique_id = f"{vin}_image"
            self._attr_translation_key = "image"
        else:
            self._attr_unique_id = f"{vin}_image_{view}"
            # A per-view translation_key (not a placeholder) so each of the
            # small closed set of views (front/back x left/center/right) gets
            # a properly translated name instead of an English fragment
            # spliced into a template.
            self._attr_translation_key = f"image_view_{view}"
            self._attr_entity_registry_enabled_default = False
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._current_url: str | None = None
        url = self._url
        if url:
            self._current_url = url
            self._attr_image_url = url
            self._attr_image_last_updated = dt_util.utcnow()

    @property
    def _vehicle(self) -> VehicleData | None:
        return (self.coordinator.data or {}).get(self._vin)

    @property
    def _url(self) -> str | None:
        v = self._vehicle
        if not v:
            return None
        return v.image_url if self._view is None else v.image_urls.get(self._view)

    @property
    def device_info(self) -> DeviceInfo | None:
        v = self._vehicle
        return _device(v) if v else None

    @callback
    def _handle_coordinator_update(self) -> None:
        url = self._url
        if url and url != self._current_url:
            self._current_url = url
            self._attr_image_url = url
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()
