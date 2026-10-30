"""Simulated Disruptive Technologies sensors in a small kitchen: no account or hardware needed.

Two cold units (a walk-in fridge and a freezer), each with a DT temperature sensor and a
proximity sensor on the door. A simple thermal model with a thermostat-controlled compressor
produces the temperatures; the simulator POSTs them exactly as a DT Data Connector would.

    python -m coldwatch.simulator backfill --days 14 --scenario compressor-degrading
    python -m coldwatch.simulator live --scenario door-left-open --interval 5
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

import httpx

from . import dt

SCENARIOS = ["normal", "door-left-open", "power-outage", "compressor-degrading", "sensor-offline", "battery-low"]
DEFAULT_URL = "http://127.0.0.1:8000/dt/events"
HOUR, DAY = 3600, 86400
TZ = ZoneInfo(os.environ.get("COLDWATCH_TZ", "Europe/Oslo"))


@dataclass
class Device:
    device_id: str
    device_type: str
    product_number: str
    labels: dict[str, str]


@dataclass
class Unit:
    key: str
    setpoint: float
    band: float  # thermostat hysteresis
    ambient: float
    leak: float  # heat leak, per minute, times (ambient - T)
    capacity: float  # °C per minute the compressor removes at full health
    door_coeff: float  # extra heat per minute while the door is open, times (ambient - T)
    door_rate_h: float  # openings per hour during opening hours
    door_secs: tuple[float, float]
    sensor: Device
    door: Device
    temp: float = 0.0
    compressor: bool = False
    door_open: bool = False
    door_close_at: float = 0.0
    forced: bool = False
    samples: list[tuple[float, float]] = field(default_factory=list)


def make_units() -> tuple[str, list[Unit]]:
    """Same ids every run, so a live session continues the backfilled history."""
    ids = random.Random("coldwatch-sim-ids")
    project = dt.new_id(ids)

    def device(kind: str, labels: dict[str, str]) -> Device:
        product = "102081" if kind == "temperature" else "102060"  # illustrative product numbers
        return Device(dt.new_id(ids), kind, product, labels)

    fridge = Unit(
        key="fridge", setpoint=3.0, band=0.5, ambient=20.0, leak=0.002, capacity=0.15,
        door_coeff=0.07, door_rate_h=2.0, door_secs=(20, 60),
        sensor=device("temperature", {"name": "Walk-in fridge temperature", "unit": "Walk-in fridge",
                                      "role": "temperature", "max_temp": "5", "min_temp": "0"}),
        door=device("proximity", {"name": "Walk-in fridge door", "unit": "Walk-in fridge", "role": "door"}),
    )
    freezer = Unit(
        key="freezer", setpoint=-21.0, band=1.0, ambient=20.0, leak=0.0015, capacity=0.25,
        door_coeff=0.04, door_rate_h=1.0, door_secs=(15, 40),
        sensor=device("temperature", {"name": "Freezer temperature", "unit": "Freezer",
                                      "role": "temperature", "max_temp": "-18", "min_temp": "-30"}),
        door=device("proximity", {"name": "Freezer door", "unit": "Freezer", "role": "door"}),
    )
    for u in (fridge, freezer):
        u.temp = u.setpoint
    return project, [fridge, freezer]


class Scenario:
    """When things go wrong. Backfill places incidents relative to the end (recent history);
    live mode starts them a few seconds after launch."""

    def __init__(self, name: str, start: float, end: float, live: bool):
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
        self.name, self.start, self.end, self.live = name, start, end, live
        span = end - start

        def window(back: float, length: float) -> tuple[float, float]:
            a = max(start + 0.1 * span, end - back)
            return a, min(end, a + length)

        if live:
            self.outage = (start + 15, end)
            self.door = (start + 15, end)
            self.silent = (start + 30, end)
            self.battery_from = start
            self.degrade = None
        else:
            self.outage = window(36 * HOUR, 2 * HOUR)
            self.door = window(20 * HOUR, 25 * 60)
            self.silent = window(30 * HOUR, 3 * HOUR)
            self.battery_from = window(24 * HOUR, 0)[0]
            self.degrade = (end - min(7 * DAY, 0.5 * span), end)

    def capacity(self, unit: Unit, t: float) -> float:
        if unit.key != "fridge":
            return 1.0
        if self.name == "power-outage" and self.outage[0] <= t < self.outage[1]:
            return 0.0
        if self.name == "compressor-degrading":
            if self.live:
                return 0.55
            a, b = self.degrade
            return 1.0 if t < a else 1.0 - 0.45 * min(1.0, (t - a) / (b - a))
        return 1.0

    def forced_open(self, unit: Unit, t: float) -> bool:
        return self.name == "door-left-open" and unit.key == "fridge" and self.door[0] <= t < self.door[1]

    def sensor_silent(self, unit: Unit, t: float) -> bool:
        return self.name == "sensor-offline" and unit.key == "freezer" and self.silent[0] <= t < self.silent[1]

    def battery(self, unit: Unit, t: float) -> tuple[int, str, float]:
        if self.name == "battery-low" and unit.key == "freezer" and t >= self.battery_from:
            return 8, "LOW", 0.6
        return 90, "OK", 9.5


class Simulation:
    def __init__(self, scenario: Scenario, seed: int, heartbeat_s: float, sample_s: float,
                 step_s: float = 10.0, speed: float = 1.0):
        self.scenario = scenario
        self.rng = random.Random(seed)
        self.heartbeat_s, self.sample_s, self.step_s, self.speed = heartbeat_s, sample_s, step_s, speed
        self.project, self.units = make_units()
        self.connector = "ccon" + dt.new_id(random.Random("coldwatch-ccon"))[:16]

    def _payload(self, device: Device, event_type: str, data: dict, ts: float) -> dict:
        return dt.make_payload(
            project_id=self.project, device_id=device.device_id, event_type=event_type, data=data,
            timestamp=ts + 0.2, labels=device.labels, device_type=device.device_type,
            product_number=device.product_number, event_id=dt.new_id(self.rng),
        )

    def _network(self, device: Device, ts: float) -> dict:
        signal = self.rng.randint(55, 90)
        return self._payload(device, "networkStatus", dt.network_data(ts, signal, -100 + signal // 2, self.connector), ts)

    def _battery(self, unit: Unit, device: Device, ts: float) -> dict:
        pct, state, years = self.scenario.battery(unit, ts) if device is unit.sensor else (92, "OK", 10.0)
        return self._payload(device, "batteryStatus", dt.battery_data(pct, state, ts, years), ts)

    def _open_hours(self, ts: float) -> bool:
        return 7 <= datetime.fromtimestamp(ts, TZ).hour < 22

    def _advance(self, u: Unit, t: float, dt_s: float) -> bool:
        """Advance one unit by dt_s. Returns True if the door changed state."""
        sc, was_open = self.scenario, u.door_open
        forced = sc.forced_open(u, t)
        if forced:
            u.door_open = True
        elif u.forced:  # forced window just ended
            u.door_open = False
        elif u.door_open and t >= u.door_close_at:
            u.door_open = False
        elif not u.door_open and self._open_hours(t) and self.rng.random() < u.door_rate_h * dt_s / HOUR:
            u.door_open = True
            u.door_close_at = t + self.rng.uniform(*u.door_secs)
        u.forced = forced

        if u.temp > u.setpoint + u.band:
            u.compressor = True
        elif u.temp < u.setpoint - u.band:
            u.compressor = False
        minutes = dt_s * self.speed / 60
        heat = u.leak * (u.ambient - u.temp)
        if u.door_open:
            heat += u.door_coeff * (u.ambient - u.temp)
        cool = u.capacity * sc.capacity(u, t) if u.compressor else 0.0
        u.temp += (heat - cool) * minutes
        return u.door_open != was_open

    def events(self, start: float, end: float) -> Iterator[tuple[float, dict]]:
        """(timestamp, Data Connector body) in time order."""
        # Let the thermostat settle into its normal cycle before anything is reported.
        for i in range(int(HOUR / self.step_s)):
            for u in self.units:
                self._advance(u, start - HOUR + i * self.step_s, self.step_s)
                u.door_open = False
        battery_every = DAY if not self.scenario.live else 20 * self.heartbeat_s
        for u in self.units:
            for d in (u.sensor, u.door):
                yield start, self._battery(u, d, start)
                yield start, self._network(d, start)

        t, next_sample, next_hb, next_battery, beats = start, start, start + self.heartbeat_s, start + battery_every, 0
        while t < end:
            t += self.step_s
            for u in self.units:
                if self._advance(u, t, self.step_s):
                    yield t, self._payload(u.door, "objectPresent", dt.object_present_data(u.door_open, t), t)
            if t >= next_sample:
                next_sample += self.sample_s
                for u in self.units:
                    if not self.scenario.sensor_silent(u, t):
                        u.samples.append((t, u.temp + self.rng.gauss(0, 0.05)))
            if t >= next_hb:
                next_hb += self.heartbeat_s
                beats += 1
                for u in self.units:
                    if u.samples and not self.scenario.sensor_silent(u, t):
                        yield t, self._payload(u.sensor, "temperature", dt.temperature_data(u.samples), t)
                        u.samples = []
                        if beats % 4 == 0:
                            yield t, self._network(u.sensor, t)
                    yield t, self._network(u.door, t)
            if t >= next_battery:
                next_battery += battery_every
                for u in self.units:
                    for d in (u.sensor, u.door):
                        if not (d is u.sensor and self.scenario.sensor_silent(u, t)):
                            yield t, self._battery(u, d, t)


def backfill_events(days: float, scenario: str, end: float | None = None, seed: int = 42) -> Iterator[dict]:
    """History with DT's default cadence: a temperature event every 15 min carrying 15 samples."""
    end = time.time() if end is None else end
    start = end - days * DAY
    sim = Simulation(Scenario(scenario, start, end, live=False), seed, heartbeat_s=900, sample_s=60)
    for _, payload in sim.events(start, end):
        yield payload


class Sender:
    def __init__(self, url: str, secret: str | None, dup_rate: float, seed: int):
        self.url, self.secret, self.dup_rate = url, secret, dup_rate
        self.client = httpx.Client(timeout=10)
        self.rng = random.Random(seed + 1)
        self.counts: Counter = Counter()

    def send(self, payload: dict) -> None:
        body = dt.encode_body(payload)
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Dt-Signature"] = dt.sign_body(body, self.secret)
        copies = 2 if self.rng.random() < self.dup_rate else 1  # DT delivers at least once
        for _ in range(copies):
            response = self.client.post(self.url, content=body, headers=headers)
            if response.status_code >= 300:
                self.counts[f"http {response.status_code}"] += 1
                print(f"  ! {response.status_code}: {response.text[:200]}", file=sys.stderr)
            else:
                self.counts[response.json().get("status", "ok")] += 1


def _summary(counts: Counter, elapsed: float) -> str:
    total = sum(counts.values())
    parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return f"{total} requests in {elapsed:.1f}s ({parts})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coldwatch.simulator", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--scenario", choices=SCENARIOS, default="normal")
    common.add_argument("--url", default=DEFAULT_URL, help="Data Connector endpoint (default %(default)s)")
    common.add_argument("--secret", default=os.environ.get("DT_SIGNATURE_SECRET"),
                        help="sign requests like a Data Connector (default: $DT_SIGNATURE_SECRET)")
    common.add_argument("--seed", type=int, default=42)
    common.add_argument("--dup-rate", type=float, default=0.01, help="fraction of events re-sent (default 1%%)")

    b = sub.add_parser("backfill", parents=[common], help="send past days of history as fast as possible")
    b.add_argument("--days", type=float, default=14)

    lv = sub.add_parser("live", parents=[common], help="send events in real time")
    lv.add_argument("--interval", type=float, default=5, help="seconds between heartbeats (DT default is 15 min)")
    lv.add_argument("--duration", type=float, default=600, help="seconds to run (default %(default)s)")
    lv.add_argument("--speed", type=float, default=1.0, help="speed up the thermal model, e.g. 5")
    args = parser.parse_args(argv)

    sender = Sender(args.url, args.secret, args.dup_rate, args.seed)
    began = time.time()
    try:
        if args.mode == "backfill":
            print(f"Backfilling {args.days:g} days, scenario '{args.scenario}' -> {args.url}")
            for i, payload in enumerate(backfill_events(args.days, args.scenario, seed=args.seed), 1):
                sender.send(payload)
                if i % 1000 == 0:
                    stamp = payload["event"]["timestamp"][:16].replace("T", " ")
                    print(f"  {i} events (simulated time {stamp} UTC)")
        else:
            start = time.time()
            end = start + args.duration
            sim = Simulation(Scenario(args.scenario, start, end, live=True), args.seed,
                             heartbeat_s=args.interval, sample_s=args.interval, step_s=1.0, speed=args.speed)
            print(f"Live: scenario '{args.scenario}', heartbeat every {args.interval:g}s for "
                  f"{args.duration:g}s -> {args.url} (Ctrl-C to stop)")
            for ts, payload in sim.events(start, end):
                delay = ts - time.time()
                if delay > 0:
                    time.sleep(delay)
                sender.send(payload)
                if payload["event"]["eventType"] in ("temperature", "objectPresent"):
                    data = payload["event"]["data"]
                    name = payload["labels"].get("name", "?")
                    value = data["temperature"]["value"] if "temperature" in data else data["objectPresent"]["state"]
                    print(f"  {datetime.fromtimestamp(ts).strftime('%H:%M:%S')} {name}: {value}")
    except httpx.HTTPError as exc:
        print(f"Could not reach {args.url}: {exc}. Is the server running?", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
    print(_summary(sender.counts, time.time() - began))
    return 0


if __name__ == "__main__":
    sys.exit(main())
