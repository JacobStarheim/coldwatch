"""The rule engine: turns DT events into readings, door cycles and alerts.

Time comes from the events themselves (when the sample was taken), not from when the request
arrived. That keeps the rules correct when DT redelivers old events after an outage and lets
the simulator backfill two weeks of history in seconds. Only checks that need "nothing has
happened for a while" (door still open, sensor silent, nobody acknowledged) run on the wall
clock, and only for sensors that are currently live.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from typing import Callable

from . import maintenance
from .config import Settings
from .db import Database
from .dt import HEARTBEAT_TYPES, ParsedEvent, parse_payload
from .notify import Notification

EXCURSION_KINDS = ("temp_high", "temp_low")
DEFAULT_HEARTBEAT_S = 15 * 60
_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-") or "unit"


def _label_float(labels: dict[str, str], key: str) -> float | None:
    try:
        raw = labels.get(key, "").strip()
        return float(raw) if raw else None
    except ValueError:
        return None


def _guess_role(ev: ParsedEvent) -> str | None:
    if ev.event_type == "objectPresent" or ev.device_type == "proximity":
        return "door"
    if ev.event_type in ("temperature", "humidity") or ev.device_type in ("temperature", "humidity"):
        return "temperature"
    return None


class Engine:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], float] = time.time):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.lock = threading.RLock()
        self._outbox: list[Notification] = []
        self._pending: list[Notification] = []

    # --- Public API -------------------------------------------------------------------------

    def ingest(self, payload: dict) -> str:
        """Process one Data Connector request body. Returns 'ok', 'duplicate' or 'ignored'.

        Raises PayloadError for bodies that are not DT events. Any other exception rolls the
        whole event back, the endpoint answers 500, and DT's retry delivers it again.
        """
        event = parse_payload(payload)
        with self.lock:
            self._pending = []
            with self.db.transaction():
                seen = self.db.one("SELECT 1 FROM processed_events WHERE event_id=?", (event.event_id,))
                if seen:
                    status = "duplicate"  # DT delivers at least once; retries must be harmless
                else:
                    status = "ok" if event.supported else "ignored"
                    if event.supported:
                        self._process(event)
                    self.db.execute(
                        "INSERT INTO processed_events VALUES (?, ?, ?, ?)",
                        (event.event_id, self.clock(), event.event_type, event.device_id),
                    )
            self._outbox.extend(self._pending)
            return status

    def tick(self, now: float | None = None) -> None:
        """Wall-clock checks: doors left open, silent sensors, excursions, escalation."""
        now = self.clock() if now is None else now
        with self.lock:
            self._pending = []
            with self.db.transaction():
                live_units = {
                    r["unit_id"] for r in self.db.all("SELECT DISTINCT unit_id FROM sensors WHERE last_live=1")
                }
                self._check_timers(now, live_units)
                self._check_offline(now)
                self._escalate(now)
            self._outbox.extend(self._pending)

    def acknowledge(self, alert_id: int, name: str, action: str) -> dict | None:
        with self.lock, self.db.transaction():
            row = self.db.one("SELECT id FROM alerts WHERE id=?", (alert_id,))
            if not row:
                return None
            self.db.execute(
                "UPDATE alerts SET acked_by=?, acked_at=?, action=? WHERE id=?",
                (name.strip(), self.clock(), action.strip(), alert_id),
            )
            return dict(self.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,)))

    @property
    def has_notifications(self) -> bool:
        return bool(self._outbox)

    def drain_notifications(self) -> list[Notification]:
        with self.lock:
            out, self._outbox = self._outbox, []
            return out

    # --- Event processing -------------------------------------------------------------------

    def _process(self, ev: ParsedEvent) -> None:
        sensor = self._upsert_sensor(ev)
        unit_id = sensor["unit_id"]
        live = self.clock() - ev.time <= self.settings.historical_s
        self._update_presence(sensor, ev, live)

        if ev.event_type in ("temperature", "humidity"):
            for sample in ev.samples:
                self._on_temperature(unit_id, ev.device_id, sample.ts, sample.value)
        elif ev.event_type == "objectPresent":
            self._on_door(unit_id, ev.device_id, ev.time, bool(ev.door_open))
        elif ev.event_type == "batteryStatus":
            self._on_battery(sensor, ev)
        elif ev.event_type == "networkStatus":
            self.db.execute(
                "UPDATE sensors SET signal_strength=? WHERE device_id=?", (ev.signal_strength, ev.device_id)
            )
        self._check_timers(ev.time, {unit_id})

    def _upsert_sensor(self, ev: ParsedEvent):
        row = self.db.one("SELECT * FROM sensors WHERE device_id=?", (ev.device_id,))
        labels = ev.labels
        role = labels.get("role") or (row["role"] if row else None) or _guess_role(ev)

        unit_name = labels.get("unit")
        if not unit_name and row and row["unit_id"]:
            unit_id = row["unit_id"]
        else:
            # Without a `unit` label each sensor becomes its own unit, named after the sensor.
            unit_name = unit_name or labels.get("name") or ev.device_id
            unit_id = slugify(unit_name)
            self.db.execute(
                "INSERT INTO units (id, name, min_temp, max_temp) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (unit_id, unit_name, self.settings.default_min_temp, self.settings.default_max_temp),
            )
        if role == "temperature":
            for key, column in (("max_temp", "max_temp"), ("min_temp", "min_temp")):
                value = _label_float(labels, key)
                if value is not None:
                    self.db.execute(f"UPDATE units SET {column}=? WHERE id=?", (value, unit_id))

        name = labels.get("name") or (row["name"] if row else None) or ev.device_id
        self.db.execute(
            """INSERT INTO sensors (device_id, project_id, unit_id, role, name, device_type, product_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                 project_id=excluded.project_id, unit_id=excluded.unit_id, role=excluded.role,
                 name=excluded.name,
                 device_type=COALESCE(excluded.device_type, sensors.device_type),
                 product_number=COALESCE(excluded.product_number, sensors.product_number)""",
            (ev.device_id, ev.project_id, unit_id, role, name, ev.device_type, ev.product_number),
        )
        return self.db.one("SELECT * FROM sensors WHERE device_id=?", (ev.device_id,))

    def _offline_threshold(self, sensor) -> float:
        heartbeat = sensor["heartbeat_s"] or DEFAULT_HEARTBEAT_S
        return max(self.settings.offline_min_s, 1.5 * heartbeat)  # DT's own rule

    def _update_presence(self, sensor, ev: ParsedEvent, live: bool) -> None:
        last = sensor["last_seen"]
        if last is not None and ev.time > last:
            threshold = self._offline_threshold(sensor)
            open_alert = self._find_open("offline", device_id=ev.device_id)
            if open_alert:
                self._resolve(open_alert["id"], ev.time)
            elif ev.time - last > threshold:
                # A gap we only notice now that the sensor is back (or while replaying history).
                alert_id = self._open_alert(
                    kind="offline",
                    unit_id=sensor["unit_id"],
                    device_id=ev.device_id,
                    severity="warning",
                    started_at=last,
                    detected_at=last + threshold,
                    message=f"Sensor '{sensor['name']}' was silent for {(ev.time - last) / 60:.0f} min",
                    notify=False,
                )
                self._resolve(alert_id, ev.time)

        heartbeat, last_hb = sensor["heartbeat_s"], sensor["last_heartbeat"]
        if ev.event_type in HEARTBEAT_TYPES:
            if last_hb is not None and ev.time > last_hb:
                interval = ev.time - last_hb
                # Ignore same-moment events and gaps from outages when learning the heartbeat.
                if interval >= 30 and (heartbeat is None or interval <= 3 * heartbeat):
                    heartbeat = interval if heartbeat is None else 0.8 * heartbeat + 0.2 * interval
            last_hb = ev.time if last_hb is None else max(last_hb, ev.time)

        self.db.execute(
            """UPDATE sensors SET last_seen=MAX(COALESCE(last_seen, 0), ?), last_live=?,
                 last_heartbeat=?, heartbeat_s=? WHERE device_id=?""",
            (ev.time, int(live), last_hb, heartbeat, ev.device_id),
        )

    def _unit(self, unit_id: str):
        return self.db.one("SELECT * FROM units WHERE id=?", (unit_id,))

    def _on_temperature(self, unit_id: str, device_id: str, ts: float, value: float) -> None:
        self.db.execute("INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?)", (device_id, unit_id, ts, value))
        unit = self._unit(unit_id)
        kind = None
        if unit["max_temp"] is not None and value > unit["max_temp"]:
            kind = "temp_high"
        elif unit["min_temp"] is not None and value < unit["min_temp"]:
            kind = "temp_low"

        state = self.db.one("SELECT * FROM unit_state WHERE unit_id=?", (unit_id,))
        current = state["breach_kind"] if state else None
        if kind is None:
            if current:
                self._set_breach(unit_id, None, None, None)
                for alert in self._find_open_all(EXCURSION_KINDS, unit_id):
                    self._resolve(alert["id"], ts)
        else:
            if current != kind:
                for alert in self._find_open_all(EXCURSION_KINDS, unit_id):
                    self._resolve(alert["id"], ts)
                since, peak = ts, value
            else:
                since = state["breach_since"]
                peak = max(state["breach_peak"], value) if kind == "temp_high" else min(state["breach_peak"], value)
            self._set_breach(unit_id, kind, since, peak)
            alert = self._find_open(kind, unit_id=unit_id)
            if alert:
                self.db.execute("UPDATE alerts SET peak=? WHERE id=?", (peak, alert["id"]))
            elif ts - since >= self.settings.excursion_grace_s:
                self._open_excursion(unit, kind, since, ts, peak)
        self._track_recovery(unit_id, ts, value)

    def _set_breach(self, unit_id, kind, since, peak) -> None:
        self.db.execute(
            "INSERT INTO unit_state VALUES (?, ?, ?, ?) ON CONFLICT(unit_id) DO UPDATE SET "
            "breach_kind=excluded.breach_kind, breach_since=excluded.breach_since, breach_peak=excluded.breach_peak",
            (unit_id, kind, since, peak),
        )

    def _open_excursion(self, unit, kind: str, since: float, detected: float, peak: float) -> None:
        high = kind == "temp_high"
        limit = unit["max_temp"] if high else unit["min_temp"]
        grace_min = self.settings.excursion_grace_s / 60
        self._open_alert(
            kind=kind,
            unit_id=unit["id"],
            device_id=None,
            severity="critical",
            started_at=since,
            detected_at=detected,
            peak=peak,
            message=(
                f"Temperature {'above' if high else 'below'} the {limit:.1f} °C limit for more than "
                f"{grace_min:.0f} min ({'peak' if high else 'lowest'} so far {peak:.1f} °C)"
            ),
        )

    def _on_door(self, unit_id: str, device_id: str, ts: float, is_open: bool) -> None:
        cycle = self.db.one(
            "SELECT * FROM door_cycles WHERE unit_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1",
            (unit_id,),
        )
        if is_open:
            if cycle:
                return
            pending = self.db.one(
                "SELECT baseline FROM door_cycles WHERE unit_id=? AND status='pending' "
                "ORDER BY closed_at DESC LIMIT 1",
                (unit_id,),
            )
            # The previous opening has not recovered yet: its baseline is still the truth.
            baseline = pending["baseline"] if pending else self._baseline(unit_id, ts)
            # Earlier openings only count if they recover before this one started.
            self.db.execute(
                "UPDATE door_cycles SET next_opened_at=? WHERE unit_id=? AND status='pending' AND next_opened_at IS NULL",
                (ts, unit_id),
            )
            self.db.execute(
                "INSERT INTO door_cycles (unit_id, device_id, opened_at, baseline, status) VALUES (?, ?, ?, ?, 'open')",
                (unit_id, device_id, ts, baseline),
            )
        elif cycle:
            status = "pending" if cycle["baseline"] is not None else "no_baseline"
            self.db.execute("UPDATE door_cycles SET closed_at=?, status=? WHERE id=?", (ts, status, cycle["id"]))
            alert = self._find_open("door_open", unit_id=unit_id)
            if alert:
                self._resolve(alert["id"], ts)

    def _baseline(self, unit_id: str, ts: float) -> float | None:
        rows = self.db.all(
            "SELECT value FROM readings WHERE unit_id=? AND ts BETWEEN ? AND ?", (unit_id, ts - 1800, ts)
        )
        return statistics.median(r["value"] for r in rows) if rows else None

    def _track_recovery(self, unit_id: str, ts: float, value: float) -> None:
        band = self.settings.recovery_band_c
        for c in self.db.all(
            "SELECT * FROM door_cycles WHERE unit_id=? AND status IN ('open', 'pending')", (unit_id,)
        ):
            if ts < c["opened_at"]:
                continue
            peak = value if c["peak"] is None else max(c["peak"], value)
            status, recovered_at, recovery_min, min_per_c = c["status"], None, None, None
            if status == "pending" and ts > c["closed_at"]:
                target = c["baseline"] + band
                if c["next_opened_at"] is not None and ts >= c["next_opened_at"]:
                    status = "interrupted"
                elif value <= target:
                    status, recovered_at = "recovered", ts
                    recovery_min = (ts - c["closed_at"]) / 60
                    rise = peak - target
                    if rise >= self.settings.min_rise_c:
                        min_per_c = recovery_min / rise
                elif ts - c["closed_at"] > self.settings.recovery_timeout_s:
                    status = "unrecovered"
            self.db.execute(
                "UPDATE door_cycles SET peak=?, status=?, recovered_at=?, recovery_min=?, min_per_c=? WHERE id=?",
                (peak, status, recovered_at, recovery_min, min_per_c, c["id"]),
            )
            if min_per_c is not None:
                self._assess_maintenance(unit_id, ts)

    def _assess_maintenance(self, unit_id: str, now: float) -> None:
        s = self.settings
        rows = self.db.all(
            "SELECT closed_at, min_per_c FROM door_cycles WHERE unit_id=? AND min_per_c IS NOT NULL "
            "AND closed_at >= ?",
            (unit_id, now - s.maint_history_days * 86400),
        )
        cut = now - s.maint_recent_days * 86400
        recent = [r["min_per_c"] for r in rows if r["closed_at"] >= cut]
        baseline = [r["min_per_c"] for r in rows if r["closed_at"] < cut]
        result = maintenance.assess(baseline, recent, s)
        if result is None:
            return
        alert = self._find_open("maintenance", unit_id=unit_id)
        if result.degraded:
            details = json.dumps(result.as_details())
            if alert:
                self.db.execute(
                    "UPDATE alerts SET message=?, details=? WHERE id=?", (result.message, details, alert["id"])
                )
            else:
                self._open_alert(
                    kind="maintenance",
                    unit_id=unit_id,
                    device_id=None,
                    severity="warning",
                    started_at=now,
                    detected_at=now,
                    message=result.message,
                    details=result.as_details(),
                )
        elif alert and result.cleared:
            self._resolve(alert["id"], now)

    def _on_battery(self, sensor, ev: ParsedEvent) -> None:
        state = ev.battery_state or ("LOW" if (ev.battery_pct or 100) < 10 else "OK")
        self.db.execute(
            "UPDATE sensors SET battery_pct=?, battery_state=? WHERE device_id=?",
            (ev.battery_pct, state, ev.device_id),
        )
        alert = self._find_open("battery", device_id=ev.device_id)
        if state in ("LOW", "CRITICAL"):
            severity = "critical" if state == "CRITICAL" else "warning"
            message = f"Battery {state} on '{sensor['name']}' ({ev.battery_pct}%): plan a replacement"
            if alert:
                self.db.execute(
                    "UPDATE alerts SET severity=?, message=? WHERE id=?", (severity, message, alert["id"])
                )
            else:
                self._open_alert(
                    kind="battery",
                    unit_id=sensor["unit_id"],
                    device_id=ev.device_id,
                    severity=severity,
                    started_at=ev.time,
                    detected_at=ev.time,
                    message=message,
                )
        elif alert:
            self._resolve(alert["id"], ev.time)

    # --- Time-based checks ------------------------------------------------------------------

    def _check_timers(self, now: float, unit_ids: set[str]) -> None:
        if not unit_ids:
            return
        marks = ",".join("?" * len(unit_ids))
        ids = tuple(unit_ids)
        for c in self.db.all(f"SELECT * FROM door_cycles WHERE status='open' AND unit_id IN ({marks})", ids):
            if now - c["opened_at"] >= self.settings.door_open_max_s and not self._find_open(
                "door_open", unit_id=c["unit_id"]
            ):
                self._open_alert(
                    kind="door_open",
                    unit_id=c["unit_id"],
                    device_id=c["device_id"],
                    severity="warning",
                    started_at=c["opened_at"],
                    detected_at=now,
                    message=f"Door has been open for more than {self.settings.door_open_max_s / 60:.0f} min",
                )
        for st in self.db.all(
            f"SELECT * FROM unit_state WHERE breach_kind IS NOT NULL AND unit_id IN ({marks})", ids
        ):
            if now - st["breach_since"] >= self.settings.excursion_grace_s and not self._find_open(
                st["breach_kind"], unit_id=st["unit_id"]
            ):
                self._open_excursion(
                    self._unit(st["unit_id"]), st["breach_kind"], st["breach_since"], now, st["breach_peak"]
                )

    def _check_offline(self, now: float) -> None:
        for sensor in self.db.all("SELECT * FROM sensors WHERE last_live=1 AND last_seen IS NOT NULL"):
            silent = now - sensor["last_seen"]
            if silent > self._offline_threshold(sensor) and not self._find_open(
                "offline", device_id=sensor["device_id"]
            ):
                self._open_alert(
                    kind="offline",
                    unit_id=sensor["unit_id"],
                    device_id=sensor["device_id"],
                    severity="warning",
                    started_at=sensor["last_seen"],
                    detected_at=now,
                    message=f"Sensor '{sensor['name']}' has not reported for {silent / 60:.0f} min",
                )

    def _escalate(self, now: float) -> None:
        due = self.db.all(
            "SELECT * FROM alerts WHERE status='open' AND historical=0 AND level=1 AND acked_at IS NULL "
            "AND notified_at <= ?",
            (now - self.settings.escalate_after_s,),
        )
        for alert in due:
            self.db.execute("UPDATE alerts SET level=2, escalated_at=? WHERE id=?", (now, alert["id"]))
            self._queue(alert, level=2)

    # --- Alert helpers ----------------------------------------------------------------------

    def _find_open(self, kind: str, *, unit_id: str | None = None, device_id: str | None = None):
        if device_id is not None:
            return self.db.one(
                "SELECT * FROM alerts WHERE status='open' AND kind=? AND device_id=?", (kind, device_id)
            )
        return self.db.one("SELECT * FROM alerts WHERE status='open' AND kind=? AND unit_id=?", (kind, unit_id))

    def _find_open_all(self, kinds: tuple[str, ...], unit_id: str):
        marks = ",".join("?" * len(kinds))
        return self.db.all(
            f"SELECT * FROM alerts WHERE status='open' AND unit_id=? AND kind IN ({marks})", (unit_id, *kinds)
        )

    def _open_alert(
        self,
        *,
        kind: str,
        unit_id: str | None,
        device_id: str | None,
        severity: str,
        started_at: float,
        detected_at: float,
        message: str,
        peak: float | None = None,
        details: dict | None = None,
        notify: bool = True,
    ) -> int:
        wall = self.clock()
        # Replayed history is recorded for the log, but nobody gets paged about last Tuesday.
        historical = (wall - detected_at > self.settings.historical_s) or not notify
        cur = self.db.execute(
            """INSERT INTO alerts (unit_id, device_id, kind, severity, status, started_at, detected_at,
                 peak, message, details, historical, level, notified_at)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                unit_id,
                device_id,
                kind,
                severity,
                started_at,
                detected_at,
                peak,
                message,
                json.dumps(details) if details else None,
                int(historical),
                0 if historical else 1,
                None if historical else wall,
            ),
        )
        alert_id = cur.lastrowid
        if not historical:
            self._queue(self.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,)), level=1)
        return alert_id

    def _resolve(self, alert_id: int, at: float) -> None:
        self.db.execute(
            "UPDATE alerts SET status='resolved', resolved_at=MAX(started_at, ?) WHERE id=?", (at, alert_id)
        )

    def _queue(self, alert, level: int) -> None:
        unit = self._unit(alert["unit_id"]) if alert["unit_id"] else None
        self._pending.append(
            Notification(
                alert_id=alert["id"],
                level=level,
                unit=unit["name"] if unit else "ColdWatch",
                kind=alert["kind"],
                severity=alert["severity"],
                message=alert["message"],
            )
        )
