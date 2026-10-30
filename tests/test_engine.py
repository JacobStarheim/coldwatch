import pytest
from helpers import (
    DOOR,
    T0,
    alerts,
    battery_event,
    door_event,
    minute_samples,
    network_event,
    temp_event,
)

from coldwatch.dt import PayloadError


def test_duplicate_events_are_processed_once(engine):
    payload = temp_event(T0, 3.0)
    assert engine.ingest(payload) == "ok"
    assert engine.ingest(payload) == "duplicate"
    assert engine.db.one("SELECT COUNT(*) AS n FROM readings")["n"] == 1


def test_unknown_event_type_is_acknowledged_but_ignored(engine):
    payload = temp_event(T0, 3.0)
    payload["event"]["eventType"] = "touch"
    assert engine.ingest(payload) == "ignored"
    assert engine.ingest(payload) == "duplicate"


def test_malformed_event_rolls_back(engine):
    with pytest.raises(PayloadError):
        engine.ingest({"event": {"eventId": "x"}})
    assert engine.db.one("SELECT COUNT(*) AS n FROM processed_events")["n"] == 0


def test_unit_and_limits_come_from_labels(engine):
    engine.ingest(temp_event(T0, 3.0))
    engine.ingest(door_event(T0, False))
    unit = engine.db.one("SELECT * FROM units")
    assert (unit["id"], unit["name"], unit["min_temp"], unit["max_temp"]) == ("fridge", "Fridge", 0.0, 5.0)
    roles = {r["role"] for r in engine.db.all("SELECT role FROM sensors WHERE unit_id='fridge'")}
    assert roles == {"temperature", "door"}


def test_sensor_without_labels_gets_default_limits(engine, settings):
    engine.ingest(temp_event(T0, 3.0, device="nolabels000000000001", labels={}))
    unit = engine.db.one("SELECT * FROM units")
    assert unit["name"] == "nolabels000000000001"
    assert unit["max_temp"] == settings.default_max_temp


def test_short_excursion_within_grace_period_is_ignored(engine):
    start = T0 - 3600
    engine.ingest(temp_event(0, 0, samples=minute_samples(start, [3.0] * 5 + [6.0] * 10 + [3.0] * 5)))
    assert alerts(engine) == []


def test_long_excursion_opens_alert_from_breach_start_and_resolves(engine, clock):
    start = T0 - 1800
    samples = minute_samples(start, [3.0] * 5 + [5.5, 6.0, 7.2] + [6.5] * 17)
    clock.t = samples[-1][0]
    engine.ingest(temp_event(0, 0, samples=samples))
    [alert] = alerts(engine, "temp_high")
    assert alert["status"] == "open"
    assert alert["started_at"] == pytest.approx(start + 5 * 60)
    assert alert["peak"] == pytest.approx(7.2)
    assert alert["severity"] == "critical"

    engine.ingest(temp_event(samples[-1][0] + 60, 3.5))
    [alert] = alerts(engine, "temp_high")
    assert alert["status"] == "resolved"
    assert alert["resolved_at"] - alert["started_at"] == pytest.approx(20 * 60)


def test_too_cold_is_an_excursion_too(engine):
    engine.ingest(temp_event(0, 0, samples=minute_samples(T0 - 3600, [-1.5] * 20)))
    assert [a["kind"] for a in alerts(engine)] == ["temp_low"]


def test_door_left_open_alert(engine, settings):
    engine.ingest(temp_event(T0 - 600, 3.0))
    engine.ingest(door_event(T0, True))
    engine.ingest(temp_event(T0 + 240, 4.0))  # 4 min: fine
    assert alerts(engine, "door_open") == []
    engine.ingest(temp_event(T0 + settings.door_open_max_s + 10, 5.0))  # time moves on via other events
    [alert] = alerts(engine, "door_open")
    assert alert["status"] == "open" and alert["started_at"] == T0
    engine.ingest(door_event(T0 + 420, False))
    assert alerts(engine, "door_open")[0]["status"] == "resolved"


def test_door_left_open_detected_by_wall_clock_tick(engine, clock, settings):
    engine.ingest(door_event(T0, True))
    engine.tick(T0 + settings.door_open_max_s - 1)
    assert alerts(engine, "door_open") == []
    engine.tick(T0 + settings.door_open_max_s + 1)
    assert len(alerts(engine, "door_open")) == 1


def test_silent_sensor_goes_offline_and_comes_back(engine, clock):
    for i in range(4):
        engine.ingest(network_event(T0 - 2700 + 900 * i))  # heartbeat every 15 min, last at T0
    engine.tick(T0 + 1300)
    assert alerts(engine, "offline") == []
    engine.tick(T0 + 1400)  # DT: offline after max(15 min, 1.5 x 15 min) = 22.5 min
    [alert] = alerts(engine, "offline")
    assert alert["status"] == "open" and alert["started_at"] == T0
    clock.t = T0 + 1500
    engine.ingest(network_event(T0 + 1500))
    assert alerts(engine, "offline")[0]["status"] == "resolved"


def test_gap_in_history_is_logged_as_offline_without_paging(engine, clock):
    old = T0 - 5 * 86400
    engine.ingest(network_event(old))
    engine.ingest(network_event(old + 900))
    engine.ingest(network_event(old + 900 + 3 * 3600))
    [alert] = alerts(engine, "offline")
    assert alert["status"] == "resolved" and alert["historical"] == 1
    assert engine.drain_notifications() == []


def test_battery_low_opens_and_ok_resolves(engine):
    engine.ingest(battery_event(T0, 8, "LOW"))
    [alert] = alerts(engine, "battery")
    assert alert["status"] == "open" and alert["severity"] == "warning"
    engine.ingest(battery_event(T0 + 60, 3, "CRITICAL"))
    assert alerts(engine, "battery")[0]["severity"] == "critical"
    engine.ingest(battery_event(T0 + 120, 100, "OK"))
    assert alerts(engine, "battery")[0]["status"] == "resolved"


def test_live_alert_notifies_staff_then_escalates(engine, clock, settings):
    samples = minute_samples(T0 - 1200, [7.0] * 21)
    engine.ingest(temp_event(0, 0, samples=samples))
    [note] = engine.drain_notifications()
    assert (note.level, note.kind, note.unit) == (1, "temp_high", "Fridge")

    engine.tick(T0 + settings.escalate_after_s - 5)
    assert engine.drain_notifications() == []
    engine.tick(T0 + settings.escalate_after_s + 5)
    [note] = engine.drain_notifications()
    assert note.level == 2 and note.title.startswith("ESCALATED")
    engine.tick(T0 + 2 * settings.escalate_after_s + 5)
    assert engine.drain_notifications() == []  # only once


def test_acknowledged_alert_is_not_escalated(engine, settings):
    engine.ingest(temp_event(0, 0, samples=minute_samples(T0 - 1200, [7.0] * 21)))
    engine.drain_notifications()
    [alert] = alerts(engine)
    row = engine.acknowledge(alert["id"], "Kari", "Moved stock to the other fridge, called service")
    assert row["acked_by"] == "Kari"
    engine.tick(T0 + settings.escalate_after_s + 5)
    assert engine.drain_notifications() == []


def test_historical_events_never_page(engine):
    engine.ingest(temp_event(0, 0, samples=minute_samples(T0 - 86400, [7.0] * 30)))
    [alert] = alerts(engine)
    assert alert["historical"] == 1 and alert["level"] == 0
    assert engine.drain_notifications() == []
    engine.tick(T0 + 3600)
    assert engine.drain_notifications() == []


def test_door_recovery_time_and_pull_down_speed(engine):
    t = T0 - 7200
    engine.ingest(temp_event(0, 0, samples=minute_samples(t, [3.0] * 30)))  # baseline 3.0
    engine.ingest(door_event(t + 1800, True, labels=DOOR))
    engine.ingest(door_event(t + 1860, False, labels=DOOR))
    # Warms to 4.5 °C, then needs 8 minutes after the door closed to get back to 3.5 °C.
    after = minute_samples(t + 1800, [3.8, 4.5, 4.4, 4.2, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5, 3.2])
    engine.ingest(temp_event(0, 0, samples=after))
    cycle = engine.db.one("SELECT * FROM door_cycles")
    assert cycle["status"] == "recovered"
    assert cycle["baseline"] == pytest.approx(3.0)
    assert cycle["peak"] == pytest.approx(4.5)
    assert cycle["recovery_min"] == pytest.approx(8.0)
    assert cycle["min_per_c"] == pytest.approx(8.0)  # 8 min to remove 1.0 °C


def test_opening_again_before_recovery_interrupts_the_measurement(engine):
    t = T0 - 7200
    engine.ingest(temp_event(0, 0, samples=minute_samples(t, [3.0] * 30)))
    engine.ingest(door_event(t + 1800, True))
    engine.ingest(door_event(t + 1830, False))
    engine.ingest(door_event(t + 1920, True))  # second opening, 1.5 min later
    engine.ingest(door_event(t + 1950, False))
    engine.ingest(temp_event(0, 0, samples=minute_samples(t + 1800, [4.5, 4.4, 4.6, 4.3, 3.9, 3.4, 3.2])))
    statuses = [r["status"] for r in engine.db.all("SELECT status FROM door_cycles ORDER BY id")]
    assert statuses == ["interrupted", "recovered"]
