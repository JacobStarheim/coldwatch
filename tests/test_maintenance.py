import json

import pytest
from helpers import T0, alerts

from coldwatch import maintenance
from coldwatch.config import Settings
from coldwatch.db import Database
from coldwatch.engine import Engine
from coldwatch.simulator import backfill_events


def run_backfill(scenario: str, days: float = 14) -> Engine:
    engine = Engine(Database(":memory:"), Settings(db_path=":memory:"), clock=lambda: T0)
    for payload in backfill_events(days, scenario, end=T0, seed=42):
        engine.ingest(payload)
    return engine


@pytest.fixture(scope="module")
def degrading():
    return run_backfill("compressor-degrading")


@pytest.fixture(scope="module")
def normal():
    return run_backfill("normal")


def test_assess_needs_enough_samples():
    s = Settings()
    assert maintenance.assess([9.0] * 5, [15.0] * 5, s) is None


def test_assess_flags_clearly_slower_pull_down():
    s = Settings()
    result = maintenance.assess([9.0] * 30, [14.0] * 30, s)
    assert result.degraded and not result.cleared
    assert result.ratio == pytest.approx(14 / 9)
    assert "+56%" in result.message


def test_assess_ignores_small_changes_and_clears_with_hysteresis():
    s = Settings()
    small = maintenance.assess([9.0] * 30, [10.0] * 30, s)
    assert not small.degraded and small.cleared
    middle = maintenance.assess([9.0] * 30, [12.0] * 30, s)  # 1.33x: not bad enough to open, not good enough to clear
    assert not middle.degraded and not middle.cleared


def test_degrading_compressor_is_flagged(degrading):
    [warning] = [a for a in alerts(degrading, "maintenance") if a["status"] == "open"]
    assert warning["unit_id"] == "walk-in-fridge"
    details = json.loads(warning["details"])
    assert details["ratio"] >= 1.5
    assert details["n_recent"] >= 20 and details["n_baseline"] >= 20
    assert warning["historical"] == 1  # replayed history: logged, not paged


def test_warning_comes_before_the_unit_starts_failing(degrading):
    [warning] = alerts(degrading, "maintenance")
    late = [a for a in alerts(degrading) if a["kind"] == "temp_high" and a["started_at"] > T0 - 7 * 86400]
    assert all(a["started_at"] > warning["detected_at"] for a in late)


def test_healthy_kitchen_raises_no_alerts(normal):
    assert alerts(normal) == []
    recovered = normal.db.one(
        "SELECT COUNT(*) AS n FROM door_cycles WHERE unit_id='walk-in-fridge' AND min_per_c IS NOT NULL"
    )["n"]
    assert recovered > 100  # plenty of door openings were measured


def test_backfill_events_are_unique_and_time_ordered():
    payloads = list(backfill_events(1, "normal", end=T0, seed=1))
    ids = [p["event"]["eventId"] for p in payloads]
    assert len(ids) == len(set(ids))
    stamps = [p["event"]["timestamp"] for p in payloads]
    assert stamps == sorted(stamps)
