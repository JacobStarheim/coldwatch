"""SQLite storage. One connection, guarded by the engine's lock."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    min_temp REAL,
    max_temp REAL
);
CREATE TABLE IF NOT EXISTS sensors (
    device_id TEXT PRIMARY KEY,
    project_id TEXT,
    unit_id TEXT,
    role TEXT,
    name TEXT,
    device_type TEXT,
    product_number TEXT,
    last_seen REAL,
    last_live INTEGER NOT NULL DEFAULT 0,
    last_heartbeat REAL,
    heartbeat_s REAL,
    battery_pct INTEGER,
    battery_state TEXT,
    signal_strength INTEGER
);
CREATE TABLE IF NOT EXISTS readings (
    device_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    ts REAL NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (device_id, ts)
);
CREATE INDEX IF NOT EXISTS readings_unit_ts ON readings (unit_id, ts);
-- One row per door opening; recovery_min is how long the unit took to get cold again.
CREATE TABLE IF NOT EXISTS door_cycles (
    id INTEGER PRIMARY KEY,
    unit_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL,
    baseline REAL,
    peak REAL,
    next_opened_at REAL,
    recovered_at REAL,
    recovery_min REAL,
    min_per_c REAL,  -- recovery time per degree of warm-up: the compressor's pull-down speed
    status TEXT NOT NULL  -- open | pending | recovered | interrupted | unrecovered | no_baseline
);
CREATE INDEX IF NOT EXISTS door_cycles_unit ON door_cycles (unit_id, status, closed_at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    unit_id TEXT,
    device_id TEXT,
    kind TEXT NOT NULL,      -- temp_high | temp_low | door_open | offline | battery | maintenance
    severity TEXT NOT NULL,  -- warning | critical
    status TEXT NOT NULL,    -- open | resolved
    started_at REAL NOT NULL,
    detected_at REAL NOT NULL,
    resolved_at REAL,
    peak REAL,
    message TEXT NOT NULL,
    details TEXT,
    historical INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,  -- 0 not notified, 1 staff, 2 escalated to manager
    notified_at REAL,
    escalated_at REAL,
    acked_by TEXT,
    acked_at REAL,
    action TEXT
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (status, unit_id, kind);
-- Current out-of-range run per unit, so the grace period survives restarts.
CREATE TABLE IF NOT EXISTS unit_state (
    unit_id TEXT PRIMARY KEY,
    breach_kind TEXT,
    breach_since REAL,
    breach_peak REAL
);
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL,
    event_type TEXT,
    device_id TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """All writes for one event commit together, or not at all (so DT's retry is safe)."""
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
