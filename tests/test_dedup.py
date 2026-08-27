"""Self-check for portal/EU Data Act duplicate suppression.

Run inside the HA container (needs the integration's deps):
    python3 tests/test_dedup.py
"""

from __future__ import annotations

import pathlib
import sys

# coordinator.py uses relative imports, so it has to load as part of the package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from custom_components.volkswagen_connect import coordinator as co  # noqa: E402


def test_drops_duplicate_when_portal_owns_signal() -> None:
    values = {"odometer": 42000, "mileage.value": 42000}
    co._drop_duplicates(values, {"odometer"})
    assert values == {"odometer": 42000}, values


def test_keeps_duplicate_when_portal_never_served_it() -> None:
    """A combustion car has no portal charging signal; EU Data Act is all it has."""
    values = {"battery_state_report.soc": 80}
    co._drop_duplicates(values, {"odometer"})
    assert values == {"battery_state_report.soc": 80}, values


def test_thin_payload_does_not_resurrect_duplicate() -> None:
    """The #18 regression: portal drops a key for one poll, duplicate must stay gone."""
    owned: set[str] = set()
    # Healthy poll: portal serves the odometer, EU Data Act copy is dropped.
    owned.update({"odometer": 42000})
    healthy = {"odometer": 42000, "mileage.value": 42000}
    co._drop_duplicates(healthy, owned)
    assert "mileage.value" not in healthy, healthy
    # Next poll the portal serves nothing (owned is unchanged), EU Data Act still does.
    thin = {"mileage.value": 42010}
    co._drop_duplicates(thin, owned)
    assert thin == {}, f"duplicate came back and would spawn a dead entity: {thin}"


def test_every_table_entry_is_reachable() -> None:
    """Guards against a portal key in the table that no source can ever set."""
    known = set(co._MAINTENANCE_MAP.values())
    charging_only = set(co._PORTAL_DUPLICATES) - known
    assert charging_only, "expected charging keys outside the maintenance map"
    for portal_key, eu_fields in co._PORTAL_DUPLICATES.items():
        assert eu_fields, f"{portal_key} lists no EU Data Act field"
        assert portal_key not in eu_fields, f"{portal_key} maps to itself"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
