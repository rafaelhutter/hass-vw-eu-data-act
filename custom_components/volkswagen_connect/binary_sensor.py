"""Binary sensor platform: boolean vehicle flags with proper device classes.

The EU Data Act feed sends a few vehicle-level booleans (lock/open status) that
read far clearer as binary sensors (Locked/Unlocked, Open/Closed) than as raw
true/false text. These keys are handled here and skipped by the sensor platform.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator, VehicleData

# Boolean keys exposed as binary sensors with a device class. ``invert`` maps our
# value to HA's on/off convention (the lock device class reads on = unlocked).
# ``encoding`` selects how the raw value is decoded (see _DECODERS below);
# defaults to "bool" (VW's plain true/false fields).
BINARY_KEYS: dict[str, dict[str, Any]] = {
    "locked": {"name": "Lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True},
    "open": {"name": "Open", "device_class": BinarySensorDeviceClass.OPENING},
    # Detailed lock states for a secondary component (vs. the flat "locked"
    # aggregate above) - diagnostic, matching how the reference vag_connect
    # integration buckets its own "Coffre verrouillé"/"Capot verrouillé".
    "trunk.locked": {"name": "Trunk lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "category": EntityCategory.DIAGNOSTIC},
    "trunk.open": {"name": "Trunk", "device_class": BinarySensorDeviceClass.OPENING},
    "parking_brake": {"name": "Parking brake", "encoding": "onoff", "category": EntityCategory.DIAGNOSTIC},
    "parking_lights": {"name": "Parking lights", "device_class": BinarySensorDeviceClass.LIGHT, "encoding": "lights", "category": EntityCategory.DIAGNOSTIC},
    # Per-door/component state fields from the EU Data Act "access" cluster.
    # VW encodes these as 0=unsupported, 1=invalid, 2/3=the two real states
    # (which one is "2" depends on the field - see each comment below). This
    # mapping isn't officially documented by VW; it's cross-checked against
    # the equivalent decoding in the sibling TommiG1/HA_VAG-EU-Data-Act
    # project (same EU Data Act backend, used by Audi/Skoda/Cupra/Seat too).
    # The raw code is still exposed as the `raw_value` attribute so you can
    # verify it yourself (open a door, watch it flip between 2 and 3).
    #
    # Lock states: 2 = locked, 3 = unlocked. Per-door/component detail beyond
    # the flat "locked" aggregate above - diagnostic, same reasoning as
    # trunk.locked/parking_brake above.
    "locked_state_front_left_door": {"name": "Front left door lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "locked_state_front_right_door": {"name": "Front right door lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    # VW's own dataFieldName has a double underscore here (confirmed against
    # the actual delivered payload) - every other door uses a single one.
    "locked_state__rear_left_door": {"name": "Rear left door lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "locked_state_rear_right_door": {"name": "Rear right door lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "locked_state_tailgate": {"name": "Tailgate lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "locked_state_front_engine_bonnet": {"name": "Bonnet lock", "device_class": BinarySensorDeviceClass.LOCK, "invert": True, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    # Open states: 2 = open, 3 = closed.
    "open_state_front_left_door": {"name": "Front left door", "device_class": BinarySensorDeviceClass.DOOR, "encoding": "state"},
    "open_state_front_right_door": {"name": "Front right door", "device_class": BinarySensorDeviceClass.DOOR, "encoding": "state"},
    "open_state_rear_left_door": {"name": "Rear left door", "device_class": BinarySensorDeviceClass.DOOR, "encoding": "state"},
    "open_state_rear_right_door": {"name": "Rear right door", "device_class": BinarySensorDeviceClass.DOOR, "encoding": "state"},
    "open_state_tailgate": {"name": "Tailgate", "device_class": BinarySensorDeviceClass.DOOR, "encoding": "state"},
    "open_state_front_engine_bonnet": {"name": "Bonnet", "device_class": BinarySensorDeviceClass.DOOR, "encoding": "state"},
    # Safe/latched states. The sibling project's comment claims 2=safe/3=unsafe
    # (matching the other "state" fields' 2/3 order), but on this Tiguan that
    # produced "Dangereux" on every door/hood/tailgate while fully closed and
    # locked — so this field is inverted relative to the others: observed
    # behaviour is 2 = unsafe, 3 = safe (properly latched).
    "safe_state_front_left_door": {"name": "Front left door latched", "device_class": BinarySensorDeviceClass.SAFETY, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "safe_state_front_right_door": {"name": "Front right door latched", "device_class": BinarySensorDeviceClass.SAFETY, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "safe_state_rear_left_door": {"name": "Rear left door latched", "device_class": BinarySensorDeviceClass.SAFETY, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "safe_state_rear_right_door": {"name": "Rear right door latched", "device_class": BinarySensorDeviceClass.SAFETY, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "safe_state_tailgate": {"name": "Tailgate latched", "device_class": BinarySensorDeviceClass.SAFETY, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "safe_state_front_engine_bonnet": {"name": "Bonnet latched", "device_class": BinarySensorDeviceClass.SAFETY, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    # Window lifter / sunroof open-state: 2 = open, 3 = closed. Kept primary -
    # the reference vag_connect integration shows its own window/sunroof
    # open-state entities as plain primary sensors, not diagnostic (only the
    # numeric *position%* sensors in sensor.py are diagnostic there).
    "state_front_left_door_window_lifter": {"name": "Front left window", "device_class": BinarySensorDeviceClass.WINDOW, "encoding": "state"},
    "state_front_right_door_window_lifter": {"name": "Front right window", "device_class": BinarySensorDeviceClass.WINDOW, "encoding": "state"},
    "state_rear_left_door_window_lifter": {"name": "Rear left window", "device_class": BinarySensorDeviceClass.WINDOW, "encoding": "state"},
    "state_rear_right_door_window_lifter": {"name": "Rear right window", "device_class": BinarySensorDeviceClass.WINDOW, "encoding": "state"},
    "state_sunroof_motor_hood_1": {"name": "Sunroof", "device_class": BinarySensorDeviceClass.WINDOW, "encoding": "state"},
    "state_sunroof_motor_hood_3": {"name": "Sunroof motor 3", "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    # Likely the ID.x/dotted counterpart of the flat "open_state_front_engine_bonnet"
    # above (same pattern as mileage/odometer or outdoor/outside_temperature) - kept
    # primary like its sibling rather than diagnostic, since only one of the two
    # ever populates per vehicle.
    "state_of_hood": {"name": "Hood", "device_class": BinarySensorDeviceClass.OPENING, "encoding": "state"},
    "state_service_hatch": {"name": "Service hatch (fuel/charge flap)", "device_class": BinarySensorDeviceClass.OPENING, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
    "state_spoiler": {"name": "Spoiler", "device_class": BinarySensorDeviceClass.OPENING, "encoding": "state", "category": EntityCategory.DIAGNOSTIC},
}

_TRUE = {"true", "1", "on", "yes"}
_FALSE = {"false", "0", "off", "no"}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _as_bool(value: Any) -> bool | None:
    """Coerce VW's plain true/false (string or bool) to a real bool, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    return None


def _decode_state_code(value: Any) -> bool | None:
    """Decode VW's 4-value door/lock/window/safety code: 0/1 = unsupported /
    invalid (-> unknown), 2 = the field's "active" state, 3 = its opposite."""
    iv = _as_int(value)
    if iv is None or iv in (0, 1):
        return None
    return iv == 2


def _decode_onoff(value: Any) -> bool | None:
    """0/1 int -> off/on (e.g. parking_brake).

    ID.x cars send this signal as a plain true/false instead, so fall back to
    the boolean reading rather than dropping the sensor to unavailable.
    """
    iv = _as_int(value)
    return _as_bool(value) if iv is None else bool(iv)


def _decode_lights(value: Any) -> bool | None:
    """0/1 = unsupported/invalid (-> unknown); 2 = off; 3/4/5 = on.

    Same true/false fallback as _decode_onoff for the ID.x payloads.
    """
    iv = _as_int(value)
    if iv is None:
        return _as_bool(value)
    if iv in (0, 1):
        return None
    return iv in (3, 4, 5)


_DECODERS = {
    "bool": _as_bool,
    "state": _decode_state_code,
    "onoff": _decode_onoff,
    "lights": _decode_lights,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VolkswagenConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new() -> None:
        new: list[BinarySensorEntity] = []
        for vin, vehicle in (coordinator.data or {}).items():
            for key, meta in BINARY_KEYS.items():
                if (vin, key) in known or key not in vehicle.values:
                    continue
                decode = _DECODERS[meta.get("encoding", "bool")]
                if decode(vehicle.values[key]) is None:
                    # Key present but not yet a meaningful reading (VW's own
                    # "0/1 = unsupported/invalid" placeholder) - skip creating
                    # an entity that would just sit as permanently
                    # Unavailable; re-checked on every update in case a later
                    # delivery actually reports a real state.
                    continue
                known.add((vin, key))
                new.append(VolkswagenConnectBinarySensor(coordinator, vin, key))
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


class VolkswagenConnectBinarySensor(
    CoordinatorEntity[VolkswagenConnectCoordinator], BinarySensorEntity
):
    """A boolean vehicle flag (lock / open) rendered with a device class."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: VolkswagenConnectCoordinator, vin: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._key = key
        meta = BINARY_KEYS[key]
        self._invert = meta.get("invert", False)
        self._decode = _DECODERS[meta.get("encoding", "bool")]
        self._attr_unique_id = f"{vin}_{key}"
        # Translation keys must be plain lowercase snake_case (no dots).
        self._attr_translation_key = key.replace(".", "_").lower()
        if "device_class" in meta:
            self._attr_device_class = meta["device_class"]
        if "category" in meta:
            self._attr_entity_category = meta["category"]

    @property
    def _vehicle(self) -> VehicleData | None:
        return (self.coordinator.data or {}).get(self._vin)

    @property
    def device_info(self) -> DeviceInfo | None:
        v = self._vehicle
        return _device(v) if v else None

    @property
    def is_on(self) -> bool | None:
        v = self._vehicle
        b = self._decode(v.values.get(self._key)) if v else None
        if b is None:
            return None
        return (not b) if self._invert else b

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        v = self._vehicle
        raw = v.values.get(self._key) if v else None
        return {"raw_value": raw} if raw is not None else None

    @property
    def available(self) -> bool:
        v = self._vehicle
        return super().available and v is not None and self._decode(v.values.get(self._key)) is not None
