"""HTTP layer: the Data Connector endpoint, a small JSON API, the dashboard and the report.

Run with:  uvicorn coldwatch.app:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .dt import SIGNATURE_HEADER, PayloadError, SignatureError, verify_signature
from .engine import Engine
from .notify import Dispatcher
from .report import build_report

log = logging.getLogger("coldwatch")
STATIC = Path(__file__).parent / "static"


class AckRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    action: str = Field(default="", max_length=1000)


def _alert_json(row) -> dict:
    data = dict(row)
    data["details"] = json.loads(data["details"]) if data.get("details") else None
    return data


def create_app(
    settings: Settings | None = None,
    *,
    background: bool = True,
    clock: Callable[[], float] = time.time,
    dispatcher: Dispatcher | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    engine = Engine(db, settings, clock)
    wake = asyncio.Event()

    async def background_loop() -> None:
        send = dispatcher or Dispatcher(settings)
        while True:
            try:
                await asyncio.wait_for(wake.wait(), timeout=settings.tick_s)
            except TimeoutError:
                pass
            wake.clear()
            try:
                await run_in_threadpool(engine.tick)
                notes = engine.drain_notifications()
                if notes:
                    await run_in_threadpool(send.send, notes)
            except Exception:  # keep the loop alive whatever happens
                log.exception("background tick failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(background_loop()) if background else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="ColdWatch", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings

    @app.post("/dt/events")
    async def dt_events(request: Request):
        """The Data Connector endpoint. DT needs a 2XX within 10 s or it retries."""
        body = await request.body()  # raw bytes: the signature covers exactly these
        if settings.signature_secret:
            try:
                verify_signature(body, request.headers.get(SIGNATURE_HEADER), settings.signature_secret)
            except SignatureError as exc:
                return JSONResponse({"error": exc.reason}, status_code=exc.status)
        try:
            payload = json.loads(body)
            status = await run_in_threadpool(engine.ingest, payload)
        except (ValueError, PayloadError) as exc:  # json.JSONDecodeError is a ValueError
            return JSONResponse({"error": str(exc)}, status_code=400)
        if engine.has_notifications:
            wake.set()
        return {"status": status}

    @app.get("/api/units")
    def units():
        with engine.lock:
            out = []
            for u in db.all("SELECT * FROM units ORDER BY name"):
                latest = db.one(
                    "SELECT ts, value FROM readings WHERE unit_id=? ORDER BY ts DESC LIMIT 1", (u["id"],)
                )
                door = db.one(
                    "SELECT opened_at FROM door_cycles WHERE unit_id=? AND status='open' "
                    "ORDER BY opened_at DESC LIMIT 1",
                    (u["id"],),
                )
                has_door = db.one("SELECT 1 FROM sensors WHERE unit_id=? AND role='door'", (u["id"],))
                sensors = db.all(
                    "SELECT device_id, name, role, last_seen, battery_pct, battery_state, signal_strength, "
                    "heartbeat_s FROM sensors WHERE unit_id=? ORDER BY role",
                    (u["id"],),
                )
                newest = latest["ts"] if latest else clock()
                cut = newest - settings.maint_recent_days * 86400
                # Same measure the maintenance warning uses: minutes per °C of pull-down.
                rec = db.all(
                    "SELECT closed_at, min_per_c FROM door_cycles WHERE unit_id=? AND min_per_c IS NOT NULL "
                    "AND closed_at >= ?",
                    (u["id"], newest - settings.maint_history_days * 86400),
                )
                recent = [r["min_per_c"] for r in rec if r["closed_at"] >= cut]
                before = [r["min_per_c"] for r in rec if r["closed_at"] < cut]
                out.append(
                    {
                        **dict(u),
                        "temperature": latest["value"] if latest else None,
                        "temperature_at": latest["ts"] if latest else None,
                        "door": None if not has_door else ("open" if door else "closed"),
                        "door_open_since": door["opened_at"] if door else None,
                        "sensors": [dict(s) for s in sensors],
                        "cooldown": {
                            "recent_min_per_c": statistics.median(recent) if recent else None,
                            "baseline_min_per_c": statistics.median(before) if before else None,
                            "n_recent": len(recent),
                            "n_baseline": len(before),
                        },
                    }
                )
            return out

    @app.get("/api/readings")
    def readings(
        unit: str, hours: float = Query(24, gt=0, le=24 * 90), max_points: int = Query(600, ge=10, le=5000)
    ):
        with engine.lock:
            row = db.one("SELECT * FROM units WHERE id=?", (unit,))
            if not row:
                raise HTTPException(404, "unknown unit")
            newest = db.one("SELECT MAX(ts) AS ts FROM readings WHERE unit_id=?", (unit,))["ts"]
            end = max(newest or 0, clock())
            start = end - hours * 3600
            bucket = max(60.0, (end - start) / max_points)
            rows = db.all(
                "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, MIN(ts) AS t, AVG(value) AS v, MAX(value) AS hi "
                "FROM readings WHERE unit_id=? AND ts >= ? GROUP BY b ORDER BY b",
                (start, bucket, unit, start),
            )
            return {
                "unit": unit,
                "min_temp": row["min_temp"],
                "max_temp": row["max_temp"],
                "points": [{"t": r["t"], "v": round(r["v"], 2), "max": round(r["hi"], 2)} for r in rows],
            }

    @app.get("/api/alerts")
    def alerts(status: str = Query("open", pattern="^(open|resolved|all)$"), limit: int = Query(100, le=1000)):
        where = "" if status == "all" else "WHERE a.status = ?"
        params: tuple = () if status == "all" else (status,)
        with engine.lock:
            rows = db.all(
                f"SELECT a.*, u.name AS unit_name FROM alerts a LEFT JOIN units u ON u.id = a.unit_id {where} "
                "ORDER BY a.status = 'open' DESC, a.detected_at DESC LIMIT ?",
                (*params, limit),
            )
            return [_alert_json(r) for r in rows]

    @app.post("/api/alerts/{alert_id}/ack")
    def acknowledge(alert_id: int, body: AckRequest):
        row = engine.acknowledge(alert_id, body.name, body.action)
        if row is None:
            raise HTTPException(404, "unknown alert")
        return _alert_json(row)

    @app.get("/report", response_class=HTMLResponse)
    def report(days: int = Query(7, ge=1, le=90)):
        with engine.lock:
            return build_report(db, settings, days=days, now=clock())

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def _lazy_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


def __getattr__(name: str):
    # `uvicorn coldwatch.app:app` builds the app on first access, so importing this module
    # (e.g. from tests) never opens the default database.
    if name == "app":
        globals()["app"] = _lazy_app()
        return globals()["app"]
    raise AttributeError(name)
