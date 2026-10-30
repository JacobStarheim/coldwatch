"""Disruptive Technologies (DT) Data Connector formats.

Parsing and signature verification for what a DT Data Connector POSTs, plus builders that
produce the same shapes (used by the simulator and the tests).

Reference: https://docs.developer.disruptive-technologies.com (Data Connectors, Events).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import jwt

SIGNATURE_HEADER = "x-dt-signature"
SUPPORTED_TYPES = {"temperature", "humidity", "objectPresent", "batteryStatus", "networkStatus"}
# Event types that arrive on a fixed schedule and therefore tell us the sensor's heartbeat.
HEARTBEAT_TYPES = {"temperature", "humidity", "networkStatus"}

_TARGET = re.compile(r"projects/([^/]+)/devices/([^/]+)")
_FRACTION = re.compile(r"\.(\d+)")
_XID_ALPHABET = "0123456789abcdefghijklmnopqrstuv"


class PayloadError(ValueError):
    """The request body is not a DT Data Connector event."""


class SignatureError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


# --- Signatures ------------------------------------------------------------------------------


def verify_signature(body: bytes, token: str | None, secret: str) -> dict:
    """Verify an X-Dt-Signature JWT against the exact raw request body.

    DT signs a JWT (HS256, the Data Connector's signature secret) whose `checksum_sha256`
    claim is the SHA-256 of the body bytes. Re-serialising the JSON would change the bytes,
    so this must run on the raw body before parsing.
    """
    if not token:
        raise SignatureError(400, "missing X-Dt-Signature header")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise SignatureError(401, f"invalid signature: {exc}") from exc
    expected = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(str(claims.get("checksum_sha256", "")), expected):
        raise SignatureError(401, "body checksum does not match signature")
    return claims


def sign_body(body: bytes, secret: str, now: float | None = None) -> str:
    """Produce a token like DT's, for the simulator and tests."""
    issued = int(now if now is not None else time.time())
    claims = {
        "checksum_sha256": hashlib.sha256(body).hexdigest(),
        "checksum": hashlib.sha1(body).hexdigest(),  # legacy claim DT still sends
        "iat": issued,
        "exp": issued + 3600,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


# --- Time -----------------------------------------------------------------------------------


def parse_time(value: str) -> float:
    """RFC 3339 -> unix seconds. Accepts 'Z' and more than 6 fractional digits."""
    s = value.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    s = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s, count=1)
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Parsing --------------------------------------------------------------------------------


@dataclass
class Sample:
    ts: float
    value: float


@dataclass
class ParsedEvent:
    event_id: str
    event_type: str
    project_id: str
    device_id: str
    time: float  # when the reading/state was taken (data.updateTime), else the event timestamp
    labels: dict[str, str] = field(default_factory=dict)
    device_type: str | None = None
    product_number: str | None = None
    samples: list[Sample] = field(default_factory=list)  # oldest first
    door_open: bool | None = None
    battery_pct: int | None = None
    battery_state: str | None = None
    signal_strength: int | None = None

    @property
    def supported(self) -> bool:
        return self.event_type in SUPPORTED_TYPES


def parse_payload(payload: dict) -> ParsedEvent:
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        raise PayloadError("body has no 'event' object")
    event_id = event.get("eventId")
    event_type = event.get("eventType")
    if not event_id or not event_type:
        raise PayloadError("event is missing eventId or eventType")
    match = _TARGET.fullmatch(str(event.get("targetName", "")))
    if not match:
        raise PayloadError("targetName is not projects/<id>/devices/<id>")

    event_time = parse_time(event["timestamp"]) if event.get("timestamp") else time.time()
    parsed = ParsedEvent(
        event_id=str(event_id),
        event_type=str(event_type),
        project_id=match.group(1),
        device_id=match.group(2),
        time=event_time,
        labels={str(k): str(v) for k, v in (payload.get("labels") or {}).items()},
    )
    if not parsed.supported:
        return parsed

    # Metadata describes the device only for device events, so read it after checking the type.
    metadata = payload.get("metadata") or {}
    parsed.device_type = metadata.get("deviceType")
    parsed.product_number = metadata.get("productNumber")

    data = (event.get("data") or {}).get(parsed.event_type)
    if not isinstance(data, dict):
        raise PayloadError(f"event data has no '{parsed.event_type}' object")
    if data.get("updateTime"):
        parsed.time = parse_time(data["updateTime"])

    try:
        if parsed.event_type in ("temperature", "humidity"):
            key = "value" if parsed.event_type == "temperature" else "temperature"
            raw = data.get("samples") or [{key: data[key], "sampleTime": data.get("updateTime")}]
            samples = [
                Sample(parse_time(s["sampleTime"]) if s.get("sampleTime") else parsed.time, float(s[key]))
                for s in raw
            ]
            parsed.samples = sorted(samples, key=lambda s: s.ts)  # DT sends newest first
        elif parsed.event_type == "objectPresent":
            state = data["state"]
            if state not in ("PRESENT", "NOT_PRESENT"):
                raise PayloadError(f"unknown objectPresent state {state!r}")
            # Proximity sensor on a door: the magnet is present when the door is closed.
            parsed.door_open = state == "NOT_PRESENT"
        elif parsed.event_type == "batteryStatus":
            parsed.battery_pct = int(data["percentage"])
            parsed.battery_state = data.get("state")
        elif parsed.event_type == "networkStatus":
            parsed.signal_strength = int(data["signalStrength"])
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PayloadError):
            raise
        raise PayloadError(f"malformed {parsed.event_type} data: {exc}") from exc
    return parsed


# --- Builders (simulator and tests) ---------------------------------------------------------


def new_id(rng: random.Random | None = None) -> str:
    """A 20-character id in the style DT uses for devices, projects and events."""
    r = rng or random
    return "".join(r.choice(_XID_ALPHABET) for _ in range(20))


def make_payload(
    *,
    project_id: str,
    device_id: str,
    event_type: str,
    data: dict,
    timestamp: float,
    labels: dict[str, str] | None = None,
    device_type: str = "temperature",
    product_number: str = "SIM-0001",
    event_id: str | None = None,
) -> dict:
    return {
        "event": {
            "eventId": event_id or new_id(),
            "targetName": f"projects/{project_id}/devices/{device_id}",
            "eventType": event_type,
            "data": {event_type: data},
            "timestamp": format_time(timestamp),
        },
        "labels": dict(labels or {}),
        "metadata": {
            "deviceId": device_id,
            "projectId": project_id,
            "deviceType": device_type,
            "productNumber": product_number,
        },
    }


def temperature_data(samples: list[tuple[float, float]]) -> dict:
    """samples: (unix ts, celsius) in any order. DT lists them newest first."""
    ordered = sorted(samples, key=lambda s: s[0], reverse=True)
    newest_ts, newest_value = ordered[0]
    return {
        "value": round(newest_value, 2),
        "samples": [{"value": round(v, 2), "sampleTime": format_time(t)} for t, v in ordered],
        "updateTime": format_time(newest_ts),
        "isBackfilled": False,
    }


def object_present_data(door_open: bool, ts: float) -> dict:
    return {"state": "NOT_PRESENT" if door_open else "PRESENT", "updateTime": format_time(ts)}


def battery_data(percentage: int, state: str, ts: float, years: float) -> dict:
    return {
        "percentage": percentage,
        "updateTime": format_time(ts),
        "state": state,
        "estimatedRemainingYears": years,
    }


def network_data(ts: float, signal: int, rssi: int, connector_id: str) -> dict:
    return {
        "signalStrength": signal,
        "rssi": rssi,
        "updateTime": format_time(ts),
        "cloudConnectors": [{"id": connector_id, "signalStrength": signal, "rssi": rssi}],
        "transmissionMode": "LOW_POWER_STANDARD_MODE",
    }


def encode_body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()
