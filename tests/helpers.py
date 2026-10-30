"""Builders for DT Data Connector bodies used across the tests."""

from __future__ import annotations

from coldwatch import dt

T0 = 1_790_000_000.0  # a fixed "now" so tests are deterministic
PROJECT = "testproject000000000"
FRIDGE = {"name": "Fridge temperature", "unit": "Fridge", "role": "temperature", "max_temp": "5", "min_temp": "0"}
DOOR = {"name": "Fridge door", "unit": "Fridge", "role": "door"}


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def temp_event(ts: float, value: float, *, samples=None, device="tempsensor0000000001", labels=FRIDGE) -> dict:
    return dt.make_payload(
        project_id=PROJECT, device_id=device, event_type="temperature",
        data=dt.temperature_data(samples or [(ts, value)]), timestamp=ts, labels=labels, device_type="temperature",
    )


def minute_samples(start: float, values: list[float]) -> list[tuple[float, float]]:
    return [(start + 60 * i, v) for i, v in enumerate(values)]


def door_event(ts: float, is_open: bool, *, device="doorsensor0000000001", labels=DOOR) -> dict:
    return dt.make_payload(
        project_id=PROJECT, device_id=device, event_type="objectPresent",
        data=dt.object_present_data(is_open, ts), timestamp=ts, labels=labels, device_type="proximity",
    )


def battery_event(ts: float, pct: int, state: str, *, device="tempsensor0000000001", labels=FRIDGE) -> dict:
    return dt.make_payload(
        project_id=PROJECT, device_id=device, event_type="batteryStatus",
        data=dt.battery_data(pct, state, ts, 0.5), timestamp=ts, labels=labels, device_type="temperature",
    )


def network_event(ts: float, *, device="tempsensor0000000001", labels=FRIDGE, device_type="temperature") -> dict:
    return dt.make_payload(
        project_id=PROJECT, device_id=device, event_type="networkStatus",
        data=dt.network_data(ts, 80, -60, "ccon0000000000000000"), timestamp=ts, labels=labels,
        device_type=device_type,
    )


def alerts(engine, kind: str | None = None) -> list[dict]:
    rows = engine.db.all("SELECT * FROM alerts ORDER BY id")
    return [dict(r) for r in rows if kind is None or r["kind"] == kind]
