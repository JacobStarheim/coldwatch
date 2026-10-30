"""HACCP-style temperature log as printable HTML (print to PDF from the browser)."""

from __future__ import annotations

import html
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .engine import DEFAULT_HEARTBEAT_S, EXCURSION_KINDS

KIND_LABELS = {
    "temp_high": "Temperature above limit",
    "temp_low": "Temperature below limit",
    "door_open": "Door left open",
    "offline": "Sensor offline",
    "battery": "Battery",
    "maintenance": "Maintenance recommended",
}

STYLE = """
body { font: 14px/1.45 -apple-system, system-ui, Segoe UI, Roboto, sans-serif; color: #1d232a; margin: 32px; background: #fff; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 8px; border-bottom: 2px solid #1d232a; padding-bottom: 4px; }
h3 { font-size: 14px; margin: 18px 0 6px; } .muted { color: #5b6672; } table { border-collapse: collapse; width: 100%; margin: 6px 0 12px; }
th, td { border: 1px solid #c9d0d6; padding: 4px 8px; text-align: left; vertical-align: top; } th { background: #eef1f4; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; } .bad { color: #b42318; font-weight: 600; }
.sign { margin-top: 36px; display: flex; gap: 48px; } .sign div { flex: 1; border-top: 1px solid #1d232a; padding-top: 4px; }
.noprint { margin-bottom: 16px; } @media print { .noprint { display: none; } body { margin: 12mm; } h2 { break-after: avoid; } table { break-inside: auto; } tr { break-inside: avoid; } }
"""


def _fmt(ts: float | None, tz: ZoneInfo, pattern: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz).strftime(pattern) if ts is not None else "–"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "ongoing"
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _downtime(stamps: list[float], threshold: float, end: float) -> list[tuple[float, float]]:
    """Intervals where the sensor should have reported but did not (DT's offline rule)."""
    gaps = []
    for a, b in zip(stamps, stamps[1:]):
        if b - a > threshold:
            gaps.append((a + threshold, b))
    if stamps and end - stamps[-1] > threshold:
        gaps.append((stamps[-1] + threshold, end))
    return gaps


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def build_report(db: Database, settings: Settings, days: int = 7, now: float | None = None) -> str:
    tz = ZoneInfo(settings.timezone)
    end = now if now is not None else time.time()
    last_day = datetime.fromtimestamp(end, tz).date()
    first_day = last_day - timedelta(days=days - 1)
    start = datetime.combine(first_day, datetime.min.time(), tz).timestamp()
    e = html.escape

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>Temperature log {first_day} – {last_day}</title><style>{STYLE}</style></head><body>",
        "<p class='noprint'><a href='/'>← Dashboard</a> · <a href='javascript:print()'>Print / save as PDF</a></p>",
        "<h1>Temperature log (HACCP)</h1>",
        f"<p class='muted'>Period {first_day} – {last_day} ({e(settings.timezone)}) · generated {_fmt(end, tz)} "
        "by ColdWatch from Disruptive Technologies sensor data. Suitable as documentation for "
        "food-safety internal control; review and sign below.</p>",
    ]

    units = db.all("SELECT * FROM units ORDER BY name")
    for unit in units:
        sensors = db.all("SELECT * FROM sensors WHERE unit_id=? AND role='temperature'", (unit["id"],))
        if not sensors:
            continue
        heartbeat = max((s["heartbeat_s"] or DEFAULT_HEARTBEAT_S) for s in sensors)
        threshold = max(settings.offline_min_s, 1.5 * heartbeat)
        readings = db.all(
            "SELECT ts, value FROM readings WHERE unit_id=? AND ts >= ? AND ts <= ? ORDER BY ts",
            (unit["id"], start - threshold, end),
        )
        stamps = [r["ts"] for r in readings]
        downtime = _downtime(stamps, threshold, end)
        first_seen = stamps[0] if stamps else end

        by_day: dict = defaultdict(list)
        for r in readings:
            if r["ts"] >= start:
                by_day[datetime.fromtimestamp(r["ts"], tz).date()].append(r["value"])

        excursions = db.all(
            f"SELECT * FROM alerts WHERE unit_id=? AND kind IN ({','.join('?' * len(EXCURSION_KINDS))}) "
            "AND COALESCE(resolved_at, ?) >= ? AND started_at <= ? ORDER BY started_at",
            (unit["id"], *EXCURSION_KINDS, end, start, end),
        )
        exc_by_day: dict = defaultdict(int)
        for a in excursions:
            exc_by_day[datetime.fromtimestamp(a["started_at"], tz).date()] += 1

        limits = []
        if unit["min_temp"] is not None:
            limits.append(f"min {unit['min_temp']:.1f} °C")
        if unit["max_temp"] is not None:
            limits.append(f"max {unit['max_temp']:.1f} °C")
        parts.append(f"<h2>{e(unit['name'])}</h2>")
        parts.append(
            f"<p class='muted'>Limits: {', '.join(limits) or 'none set'} · sensors: "
            f"{e(', '.join(s['name'] for s in sensors))} · excursions shorter than "
            f"{settings.excursion_grace_s / 60:.0f} min (e.g. door openings) are not counted.</p>"
        )
        parts.append(
            "<table><tr><th>Date</th><th class='num'>Min °C</th><th class='num'>Avg °C</th>"
            "<th class='num'>Max °C</th><th class='num'>Readings</th><th class='num'>Excursions</th>"
            "<th class='num'>Sensor uptime</th></tr>"
        )
        day = first_day
        while day <= last_day:
            d0 = datetime.combine(day, datetime.min.time(), tz).timestamp()
            d1 = min(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).timestamp(), end)
            w0 = max(d0, first_seen)
            values = by_day.get(day, [])
            if d1 > w0:
                down = sum(_overlap(g0, g1, w0, d1) for g0, g1 in downtime)
                uptime = f"{100 * (1 - down / (d1 - w0)):.1f} %"
            else:
                uptime = "–"
            n_exc = exc_by_day.get(day, 0)
            if values:
                mx = max(values)
                over = unit["max_temp"] is not None and mx > unit["max_temp"]
                cells = (
                    f"<td class='num'>{min(values):.1f}</td><td class='num'>{sum(values) / len(values):.1f}</td>"
                    f"<td class='num{' bad' if over else ''}'>{mx:.1f}</td><td class='num'>{len(values)}</td>"
                )
            else:
                cells = "<td class='num'>–</td><td class='num'>–</td><td class='num'>–</td><td class='num'>0</td>"
            parts.append(
                f"<tr><td>{day}</td>{cells}<td class='num{' bad' if n_exc else ''}'>{n_exc}</td>"
                f"<td class='num'>{uptime}</td></tr>"
            )
            day += timedelta(days=1)
        parts.append("</table>")

        parts.append("<h3>Excursion log</h3>")
        if excursions:
            parts.append(
                "<table><tr><th>Start</th><th>Duration</th><th class='num'>Peak °C</th><th>Type</th>"
                "<th>Acknowledged by</th><th>Corrective action</th></tr>"
            )
            for a in excursions:
                duration = a["resolved_at"] - a["started_at"] if a["resolved_at"] else None
                parts.append(
                    f"<tr><td>{_fmt(a['started_at'], tz)}</td><td>{_duration(duration)}</td>"
                    f"<td class='num'>{a['peak']:.1f}</td><td>{KIND_LABELS[a['kind']]}</td>"
                    f"<td>{e(a['acked_by'] or '–')}</td><td>{e(a['action'] or '–')}</td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append("<p>No excursions in this period.</p>")

    other = db.all(
        f"SELECT a.*, u.name AS unit_name FROM alerts a LEFT JOIN units u ON u.id = a.unit_id "
        f"WHERE a.kind NOT IN ({','.join('?' * len(EXCURSION_KINDS))}) "
        "AND COALESCE(a.resolved_at, ?) >= ? AND a.started_at <= ? ORDER BY a.started_at",
        (*EXCURSION_KINDS, end, start, end),
    )
    parts.append("<h2>Other events</h2>")
    if other:
        parts.append(
            "<table><tr><th>Start</th><th>Duration</th><th>Unit</th><th>Event</th><th>Details</th>"
            "<th>Acknowledged by</th><th>Action</th></tr>"
        )
        for a in other:
            duration = a["resolved_at"] - a["started_at"] if a["resolved_at"] else None
            parts.append(
                f"<tr><td>{_fmt(a['started_at'], tz)}</td><td>{_duration(duration)}</td>"
                f"<td>{e(a['unit_name'] or '–')}</td><td>{KIND_LABELS.get(a['kind'], e(a['kind']))}</td>"
                f"<td>{e(a['message'])}</td><td>{e(a['acked_by'] or '–')}</td><td>{e(a['action'] or '–')}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p>None.</p>")

    if not units:
        parts.append("<p>No sensor data yet.</p>")
    parts.append("<div class='sign'><div>Checked by (name)</div><div>Date</div><div>Signature</div></div>")
    parts.append("</body></html>")
    return "".join(parts)
