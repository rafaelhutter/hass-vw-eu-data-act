"""Calendar platform: upcoming service due-dates.

Built purely from fields the coordinator already parses (inspection_due_days,
oil_service_due_days, from website_portal.py::get_maintenance via
coordinator._MAINTENANCE_MAP) - no new API calls. Both fields are the
well-known VW "days until due" countdown (can go negative once overdue), so
the event date is simply today + that many days.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import VolkswagenConnectConfigEntry, VolkswagenConnectCoordinator, VehicleData

# values key -> event summary
_DUE_DATE_FIELDS = {
    "inspection_due_days": "Inspection due",
    "oil_service_due_days": "Oil service due",
}


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
            if vin not in known and any(k in vehicle.values for k in _DUE_DATE_FIELDS)
        ]
        known.update(due)
        if due:
            async_add_entities(
                VolkswagenConnectServiceCalendar(coordinator, vin) for vin in due
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


class VolkswagenConnectServiceCalendar(
    CoordinatorEntity[VolkswagenConnectCoordinator], CalendarEntity
):
    """Upcoming inspection/oil-service due dates for one vehicle."""

    _attr_has_entity_name = True
    _attr_name = "Service due"
    _attr_icon = "mdi:wrench-clock"

    def __init__(self, coordinator: VolkswagenConnectCoordinator, vin: str) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{vin}_service_calendar"

    @property
    def device_info(self) -> DeviceInfo | None:
        vehicle = (self.coordinator.data or {}).get(self._vin)
        return _device(vehicle) if vehicle else None

    def _events(self) -> list[CalendarEvent]:
        vehicle = (self.coordinator.data or {}).get(self._vin)
        if not vehicle:
            return []
        today = dt_util.now().date()
        events: list[CalendarEvent] = []
        for key, summary in _DUE_DATE_FIELDS.items():
            days = vehicle.values.get(key)
            if days is None:
                continue
            try:
                due = today + timedelta(days=int(days))
            except (TypeError, ValueError):
                continue
            events.append(
                CalendarEvent(
                    start=due,
                    end=due + timedelta(days=1),
                    summary=summary,
                    uid=f"{self._vin}_{key}",
                )
            )
        events.sort(key=lambda e: e.start)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        events = self._events()
        return events[0] if events else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        lo, hi = start_date.date(), end_date.date()
        return [e for e in self._events() if e.start < hi and e.end > lo]
