"""Sensor platform for the Volkswagen Connect integration.

The EU Data Act payload schema varies by enabled data clusters and is not known
ahead of time, so value sensors are created dynamically from the flattened
dataset keys (and new keys are added as they first appear). Each vehicle also
gets a stable "status" sensor that always exists, even before any content is
delivered.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .binary_sensor import BINARY_KEYS
from .const import DOMAIN
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator, VehicleData

_LOGGER = logging.getLogger(__name__)

# Hard cap on dynamically-created value sensors per vehicle. The portal data uses
# a small fixed set of clean keys; this only ever bites if the EU Data Act payload
# produces an unexpectedly wide/unstable key set. It is a backstop against runaway
# entity creation — better to drop extra keys (logged) than to flood Home Assistant.
MAX_VALUE_SENSORS_PER_VEHICLE = 100

# EU Data Act data older than this (measured at the car, not when the portal
# delivered it) is flagged stale on the Data status sensor. The portal delivers
# every 15 min, so 30 min = the car hasn't reported in two cycles.
STALE_AFTER_MINUTES = 30

def _kelvin_to_celsius(value: Any) -> Any:
    """Convert VW's scaled-Kelvin encoding to Celsius, whatever the scale.

    outside_temperature is Kelvin shifted by a power of ten, but not always the
    same one: 2991 (deci-Kelvin, #23) and 2.961 (#22) were both reported for
    this signal. Pick the scale that lands in 200-400 K, else pass it through.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return value
    for scale in (1, 0.1, 100, 10, 0.01, 1000):
        kelvin = value * scale
        if 200 <= kelvin < 400:
            return round(kelvin - 273.15, 1)
    return value


def _tenths(value: Any) -> Any:
    """VW sends trip-computer consumption in tenths (66 = 6.6 l/100km).

    Only whole numbers are scaled; a value that already carries decimals is
    one VW delivered pre-scaled and gets left alone.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return round(value / 10, 1)


# Friendly metadata for known (authproxy-derived) keys. Unknown keys still get
# a generic sensor.
KNOWN_KEYS: dict[str, dict[str, Any]] = {
    "odometer": {
        "name": "Odometer",
        "device_class": SensorDeviceClass.DISTANCE,
        "unit": UnitOfLength.KILOMETERS,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:counter",
    },
    # Flat (pre-ID.x) mileage/range fields - this Tiguan reports these instead
    # of the dotted "mileage.value"/"primary_range" equivalents above.
    "mileage": {"name": "Mileage", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.TOTAL_INCREASING, "icon": "mdi:counter"},
    "cruising_range_combined": {"name": "Range (combined)", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:map-marker-distance"},
    "cruising_range_primary_engine": {"name": "Range (primary)", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:gas-station"},
    "cruising_range_secondary_engine": {"name": "Range (secondary)", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:ev-station"},
    "fuel_level_current_level": {"name": "Fuel level", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:gas-station"},
    # Trip-computer averages, reported in tenths (#12).
    "long_term_data_average_fuel_consumption": {"name": "Average consumption (long term)", "unit": "L/100 km", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:gas-station-outline", "transform": _tenths},
    "short_term_data_average_fuel_consumption": {"name": "Average consumption (short term)", "unit": "L/100 km", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:gas-station-outline", "transform": _tenths},
    "fuel_level__accuracy": {"name": "Fuel level accuracy", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:gauge", "category": EntityCategory.DIAGNOSTIC},
    # oil_level_total_max (a 0-1 scale reference, not a liter capacity - the
    # sibling vw_eu_data_act project labels it "L" but that doesn't match a
    # real engine's oil capacity) is left uncurated; this is the meaningful
    # percentage gauge.
    "oil_level_actual_level": {"name": "Oil level", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:oil-level"},
    "cng_gas_level": {"name": "CNG gas level", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:gas-cylinder"},
    "position_front_left_door_window_lifter": {"name": "Front left window position", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:window-open-variant", "category": EntityCategory.DIAGNOSTIC},
    "position_front_right_door_window_lifter": {"name": "Front right window position", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:window-open-variant", "category": EntityCategory.DIAGNOSTIC},
    "position_rear_left_door_window_lifter": {"name": "Rear left window position", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:window-open-variant", "category": EntityCategory.DIAGNOSTIC},
    "position_rear_right_door_window_lifter": {"name": "Rear right window position", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:window-open-variant", "category": EntityCategory.DIAGNOSTIC},
    "position_sunroof_motor_hood_1": {"name": "Sunroof position", "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:car-convertible", "category": EntityCategory.DIAGNOSTIC},
    "inspection_due_days": {"name": "Inspection due", "unit": UnitOfTime.DAYS, "icon": "mdi:wrench-clock"},
    "inspection_due_km": {"name": "Inspection due", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS},
    "oil_service_due_days": {"name": "Oil service due", "unit": UnitOfTime.DAYS, "icon": "mdi:oil"},
    "oil_service_due_km": {"name": "Oil service due", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS},
    "last_report": {"name": "Last vehicle report", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-check"},
    # Vehicle health + lock history (from the authproxy)
    "warning_lights": {"name": "Warning lights", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:car-light-alert"},
    "last_lock_action": {"name": "Last lock command", "icon": "mdi:car-key", "category": EntityCategory.DIAGNOSTIC},
    "last_lock_action_time": {"name": "Last lock command time", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-outline", "category": EntityCategory.DIAGNOSTIC},
    # Live battery / charging (from charging/status)
    "soc": {"name": "Battery", "device_class": SensorDeviceClass.BATTERY, "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT},
    "electric_range": {"name": "Electric range", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:map-marker-distance"},
    "target_soc": {"name": "Target battery", "unit": PERCENTAGE, "icon": "mdi:battery-charging-high"},
    "battery_temp": {"name": "Battery temperature", "device_class": SensorDeviceClass.TEMPERATURE, "unit": UnitOfTemperature.CELSIUS, "state_class": SensorStateClass.MEASUREMENT, "category": EntityCategory.DIAGNOSTIC},
    "charging_state": {"name": "Charging state", "icon": "mdi:ev-station"},
    "charge_power": {"name": "Charge power", "device_class": SensorDeviceClass.POWER, "unit": UnitOfPower.KILO_WATT, "state_class": SensorStateClass.MEASUREMENT},
    "charge_rate": {"name": "Charge rate", "unit": UnitOfSpeed.KILOMETERS_PER_HOUR, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:speedometer"},
    "charge_time_remaining": {"name": "Charge time remaining", "device_class": SensorDeviceClass.DURATION, "unit": UnitOfTime.MINUTES, "icon": "mdi:timer-sand"},
    "charge_mode": {"name": "Charge mode", "icon": "mdi:cog"},
    "plug_connection": {"name": "Plug", "icon": "mdi:power-plug"},
    "plug_lock": {"name": "Plug lock", "icon": "mdi:lock", "category": EntityCategory.DIAGNOSTIC},
    "external_power": {"name": "External power", "icon": "mdi:transmission-tower"},
    # --- EU Data Act fields (dataFieldName keys) --------------------------------
    # Friendly labels + units for the meaningful "continuous data" signals. The
    # long tail of raw fields keeps its dotted dataFieldName as its label.
    "mileage.value": {"name": "Mileage", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.TOTAL_INCREASING, "icon": "mdi:counter"},
    "primary_range": {"name": "Primary range", "device_class": SensorDeviceClass.DISTANCE, "unit": UnitOfLength.KILOMETERS, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:map-marker-distance"},
    "battery_level_HV.value": {"name": "Battery level", "device_class": SensorDeviceClass.BATTERY, "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT},
    "battery_state_report.soc": {"name": "Battery state of charge", "device_class": SensorDeviceClass.BATTERY, "unit": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT},
    "battery_state_report.charge_power": {"name": "Charge power (report)", "device_class": SensorDeviceClass.POWER, "unit": UnitOfPower.KILO_WATT, "state_class": SensorStateClass.MEASUREMENT},
    "battery_state_report.charge_rate": {"name": "Charge rate (report)", "unit": UnitOfSpeed.KILOMETERS_PER_HOUR, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:speedometer"},
    "battery_state_report.charge_energy": {"name": "Charge energy", "device_class": SensorDeviceClass.ENERGY, "unit": UnitOfEnergy.KILO_WATT_HOUR, "icon": "mdi:lightning-bolt", "category": EntityCategory.DIAGNOSTIC},
    "battery_care_mode.charge_bcam_threshold": {"name": "Battery care threshold", "unit": PERCENTAGE, "icon": "mdi:battery-heart-variant", "category": EntityCategory.DIAGNOSTIC},
    "settings.target_soc": {"name": "Target SoC (setting)", "unit": PERCENTAGE, "icon": "mdi:battery-charging-high", "category": EntityCategory.DIAGNOSTIC},
    "outdoor_temperature": {"name": "Outdoor temperature", "device_class": SensorDeviceClass.TEMPERATURE, "unit": UnitOfTemperature.CELSIUS, "state_class": SensorStateClass.MEASUREMENT},
    # Flat (pre-ID.x) equivalent of "outdoor_temperature", reported in Kelvin.
    "outside_temperature": {"name": "Outside temperature", "device_class": SensorDeviceClass.TEMPERATURE, "unit": UnitOfTemperature.CELSIUS, "state_class": SensorStateClass.MEASUREMENT, "transform": _kelvin_to_celsius},
    # Per VW's Data Dictionary: coldest/warmest battery *module* temperature.
    "min_temperature": {"name": "Battery module min temperature", "device_class": SensorDeviceClass.TEMPERATURE, "unit": UnitOfTemperature.CELSIUS, "state_class": SensorStateClass.MEASUREMENT, "category": EntityCategory.DIAGNOSTIC},
    "max_temperature": {"name": "Battery module max temperature", "device_class": SensorDeviceClass.TEMPERATURE, "unit": UnitOfTemperature.CELSIUS, "state_class": SensorStateClass.MEASUREMENT, "category": EntityCategory.DIAGNOSTIC},
    "car_captured_time": {"name": "Car captured time", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-check", "category": EntityCategory.DIAGNOSTIC},
    "car_captured_utc_timestamp": {"name": "Car captured (UTC)", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-outline", "category": EntityCategory.DIAGNOSTIC},
    "instrument_cluster_time": {"name": "Instrument cluster time", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-outline", "category": EntityCategory.DIAGNOSTIC},
    "profile_state_report.car_captured_time": {"name": "Profile car captured time", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-check", "category": EntityCategory.DIAGNOSTIC},
    "profile_state_report.instrument_cluster_time": {"name": "Profile instrument cluster time", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-outline", "category": EntityCategory.DIAGNOSTIC},
    "profile_state_report.next_charging_timer_information.estimated_start_time": {"name": "Next charge start (est.)", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-start"},
    "profile_state_report.next_charging_timer_information.estimated_finish_time": {"name": "Next charge finish (est.)", "device_class": SensorDeviceClass.TIMESTAMP, "icon": "mdi:clock-end"},
    "battery_state_report.remaining_charging_time_complete": {"name": "Remaining charge time", "device_class": SensorDeviceClass.DURATION, "unit": UnitOfTime.SECONDS, "icon": "mdi:timer-sand", "category": EntityCategory.DIAGNOSTIC},
    "remaining_climate_time": {"name": "Remaining climate time", "device_class": SensorDeviceClass.DURATION, "unit": UnitOfTime.SECONDS, "icon": "mdi:timer-sand", "category": EntityCategory.DIAGNOSTIC},
    "locked": {"name": "Locked", "icon": "mdi:lock"},
    "open": {"name": "Open", "icon": "mdi:car-door"},
    "parking_light_left": {"name": "Parking light left", "icon": "mdi:car-parking-lights", "category": EntityCategory.DIAGNOSTIC},
    "parking_light_right": {"name": "Parking light right", "icon": "mdi:car-parking-lights", "category": EntityCategory.DIAGNOSTIC},
    "window_heating_state": {"name": "Window heating", "icon": "mdi:car-defrost-rear", "category": EntityCategory.DIAGNOSTIC},
    "additional_consumptions.residual_consumption": {"name": "Residual consumption", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:flash", "category": EntityCategory.DIAGNOSTIC},
    "additional_consumptions.interior_climatization_consumption": {"name": "Climatisation consumption", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:fan", "category": EntityCategory.DIAGNOSTIC},
    "slope_consumption_values.ascent_slope_consumption.physical_value": {"name": "Ascent slope consumption", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:trending-up", "category": EntityCategory.DIAGNOSTIC},
    "slope_consumption_values.descent_slope_consumption.physical_value": {"name": "Descent slope consumption", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:trending-down", "category": EntityCategory.DIAGNOSTIC},
    "energy_contents.current_energy_content.physical_value": {"name": "Current energy content", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:battery-charging", "category": EntityCategory.DIAGNOSTIC},
    "energy_contents.maximal_energy_content.physical_value": {"name": "Maximal energy content", "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:battery", "category": EntityCategory.DIAGNOSTIC},
    "charging_state_report.charge_type": {"name": "Charge type", "icon": "mdi:ev-plug-type2", "category": EntityCategory.DIAGNOSTIC},
    "charging_state_report.charge_mode": {"name": "Charge mode (report)", "icon": "mdi:cog", "category": EntityCategory.DIAGNOSTIC},
    "charging_state_report.current_charge_state": {"name": "Charge state (report)", "icon": "mdi:ev-station", "category": EntityCategory.DIAGNOSTIC},
    "settings.max_charge_current_ac": {"name": "Max AC charge current", "icon": "mdi:current-ac", "category": EntityCategory.DIAGNOSTIC},
    "settings.charge_mode_selection": {"name": "Charge mode selection", "icon": "mdi:cog-outline", "category": EntityCategory.DIAGNOSTIC},
}


# Leading category prefixes on VW enum values, stripped before humanising so
# CHARGE_STATE_NOT_READY_FOR_CHARGING -> "Not ready for charging". Longest /
# most-specific first (CHARGE_MODE_SELECTION_ must beat CHARGE_MODE_).
_ENUM_PREFIXES = (
    "CHARGE_MODE_SELECTION_",
    "MAX_CHARGE_CURRENT_AC_",
    "IMMEDIATE_ACTION_STATE_",
    "WINDOW_HEATING_STATE_",
    "TARGET_REACHABILITY_",
    "PLUG_CONNECTION_STATE_",
    "CHARGING_SCENARIO_",
    "BCAM_ACTIVATION_",
    "AUTO_UNLOCK_AC_",
    "PLUG_LOCK_STATE_",
    "CHARGE_RATE_UNIT_",
    "CHARGE_STATE_",
    "CHARGE_TYPE_",
    "CHARGE_MODE_",
)

# Exact-value overrides where prefix-strip + sentence-case wouldn't read well
# (acronyms, units, glued words).
_VALUE_OVERRIDES = {
    "CHARGE_TYPE_AC": "AC",
    "CHARGE_TYPE_DC": "DC",
    "CHARGE_RATE_UNIT_KM_PER_H": "km/h",
    "CHARGE_MODE_IMMEDIATELY_DEFAULT": "Immediate (default)",
    "CHARGE_MODE_SELECTION_IMMEDIATECHARGING": "Immediate charging",
    "VALID": "Valid",
    "INVALID": "Invalid",
}


def _humanize_value(value: Any) -> Any:
    """Turn a VW enum code into a readable string.

    e.g. ``CHARGE_STATE_NOT_READY_FOR_CHARGING`` -> ``Not ready for charging``,
    ``WINDOW_HEATING_STATE_OFF`` -> ``Off``. Non-enum values are returned
    unchanged; the original code stays available as the ``raw_value`` attribute.
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s in _VALUE_OVERRIDES:
        return _VALUE_OVERRIDES[s]
    # Only touch SCREAMING_SNAKE enum codes (need at least one underscore).
    if "_" not in s or not re.fullmatch(r"[A-Z][A-Z0-9_]+", s):
        return value
    body = s
    for prefix in _ENUM_PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix):
            body = s[len(prefix):]
            break
    body = body.replace("_", " ").lower()
    return body[:1].upper() + body[1:]


# Numeric *state codes* (door/lock/access state) must stay discrete, not graph as
# measurements (#12); state_of_charge/health are real gauges, so they're excluded.
_DISCRETE_STATE_RE = re.compile(
    r"(^|[._])(open|locked|safe)_state(?=[._]|$)|(^|[._])state_of_(?!charge|health)",
    re.IGNORECASE,
)


def _is_discrete_state_key(key: str) -> bool:
    return bool(_DISCRETE_STATE_RE.search(key))


def _prettify(key: str) -> str:
    """Turn a raw EU Data Act dataFieldName into a readable label.

    e.g. ``battery_state_report.remaining_charging_time_complete`` ->
    ``Battery state report remaining charging time complete``. Used for any field
    without a curated entry in KNOWN_KEYS so the UI never shows a dotted code.
    """
    words = key.replace(".", " ").replace("_", " ").split()
    label = " ".join(words)
    return label[:1].upper() + label[1:] if label else key


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VolkswagenConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    # Per-vehicle count of value sensors already created, to enforce the cap.
    value_count: dict[str, int] = {}

    @callback
    def _add_new() -> None:
        new: list[SensorEntity] = []
        for vin, vehicle in (coordinator.data or {}).items():
            status_key = (vin, "__status__")
            if status_key not in known:
                known.add(status_key)
                new.append(VolkswagenConnectStatusSensor(coordinator, vin))
                new.append(VolkswagenConnectCapturedSensor(coordinator, vin))
            for key in vehicle.values:
                vk = (vin, key)
                if vk in known or key in BINARY_KEYS:
                    continue
                # Known keys (the curated portal set) are always allowed; only
                # unrecognised keys count against the cap, so live telemetry can
                # never be starved by a noisy EU Data Act payload.
                if key not in KNOWN_KEYS and value_count.get(vin, 0) >= MAX_VALUE_SENSORS_PER_VEHICLE:
                    _LOGGER.warning(
                        "Vehicle %s already has %d dynamic sensors; skipping extra key %r "
                        "to avoid flooding Home Assistant. This usually means the EU Data Act "
                        "payload changed shape — please open an issue.",
                        vin,
                        MAX_VALUE_SENSORS_PER_VEHICLE,
                        key,
                    )
                    continue
                known.add(vk)
                if key not in KNOWN_KEYS:
                    value_count[vin] = value_count.get(vin, 0) + 1
                new.append(VolkswagenConnectValueSensor(coordinator, vin, key))
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


class _Base(CoordinatorEntity[VolkswagenConnectCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str) -> None:
        super().__init__(coordinator)
        self._vin = vin

    @property
    def _vehicle(self) -> VehicleData | None:
        return (self.coordinator.data or {}).get(self._vin)

    @property
    def device_info(self) -> DeviceInfo | None:
        v = self._vehicle
        return _device(v) if v else None


class VolkswagenConnectStatusSensor(_Base):
    """Always-present per-vehicle status (ok / no_data / not_configured)."""

    _attr_icon = "mdi:database-sync"
    _attr_translation_key = "data_status"

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_data_status"

    @property
    def native_value(self) -> StateType:
        v = self._vehicle
        return v.status if v else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        v = self._vehicle
        if not v:
            return {}
        attrs: dict[str, Any] = {
            "vin": v.vin,
            "nickname": v.info.get("nickName"),
            "license_plate": v.info.get("licensePlate"),
            "enrollment_status": v.info.get("enrollmentStatus"),
            "data_request_id": v.identifier,
            "latest_dataset": v.dataset,
            "created_on": v.created_on,
            "captured_at": v.captured_at,
        }
        # Freshness: the dataset is delivered every 15 min, but its content can be
        # hours old while the car is parked. Surface the captured-data age so a
        # stale EU reading is obvious (the live battery/charging/odometer signals
        # already fall back to the fresh portal source via the coordinator).
        captured = dt_util.parse_datetime(v.captured_at) if v.captured_at else None
        if captured is not None:
            age_min = (dt_util.utcnow() - captured).total_seconds() / 60
            attrs["data_age_minutes"] = round(age_min, 1)
            attrs["stale"] = age_min > STALE_AFTER_MINUTES
        return attrs


class VolkswagenConnectCapturedSensor(_Base):
    """When the car actually captured the latest EU Data Act data.

    The dataset is *delivered* every 15 min, but the readings inside can be hours
    old while the car is parked, so this surfaces the true measurement time. Home
    Assistant renders it as a relative age ("4 hours ago"), making a stale feed
    obvious at a glance and easy to automate on.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:car-clock"
    _attr_name = "Data captured"

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_data_captured"

    @property
    def native_value(self) -> StateType:
        v = self._vehicle
        if not v or not v.captured_at:
            return None
        return dt_util.parse_datetime(v.captured_at)


class VolkswagenConnectValueSensor(_Base):
    """A single flattened value from the latest delivered dataset."""

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str, key: str) -> None:
        super().__init__(coordinator, vin)
        self._key = key
        self._attr_unique_id = f"{vin}_{key}"
        meta = KNOWN_KEYS.get(key, {})
        self._transform = meta.get("transform")
        self._attr_name = meta.get("name") or _prettify(key)
        if "device_class" in meta:
            self._attr_device_class = meta["device_class"]
        if "unit" in meta:
            self._attr_native_unit_of_measurement = meta["unit"]
        if "state_class" in meta:
            self._attr_state_class = meta["state_class"]
        if "icon" in meta:
            self._attr_icon = meta["icon"]
        if "category" in meta:
            self._attr_entity_category = meta["category"]
        elif not meta:
            # Uncurated EU Data Act field: a raw code with no documented meaning
            # and no curated label (see _prettify above). Keep it available
            # (raw_value is still on the entity) but out of the way of the main
            # device card, and off by default so a wide continuous-data payload
            # doesn't flood new setups with unreadable sensors.
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        if not meta and self._is_numeric() and not _is_discrete_state_key(key):
            # Uncurated numeric fields would otherwise render as discrete
            # string states in HA (no graphs, no statistics) — see issue #12.
            self._attr_state_class = SensorStateClass.MEASUREMENT

    def _is_numeric(self) -> bool:
        v = self._vehicle
        val = v.values.get(self._key) if v else None
        return isinstance(val, (int, float)) and not isinstance(val, bool)

    @property
    def native_value(self) -> StateType:
        v = self._vehicle
        if not v:
            return None
        val = v.values.get(self._key)
        # Use the public accessor, not the private `_attr_device_class`: when a
        # sensor has no device class set, reading the backing attribute directly
        # raises on HA's cached-properties machinery.
        if self.device_class == SensorDeviceClass.TIMESTAMP and isinstance(val, str):
            return dt_util.parse_datetime(val)
        if isinstance(val, bool):
            return str(val).lower()
        if isinstance(val, str):
            return _humanize_value(val)
        if self._transform is not None:
            return self._transform(val)
        return val

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the original VW code when the displayed value was humanised,
        so templates/automations can still match the raw enum."""
        v = self._vehicle
        if not v:
            return None
        val = v.values.get(self._key)
        if isinstance(val, str) and _humanize_value(val) != val:
            return {"raw_value": val}
        return None

    @property
    def available(self) -> bool:
        return super().available and self._vehicle is not None
