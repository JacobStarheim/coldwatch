import hashlib
from datetime import datetime, timezone

import jwt
import pytest
from helpers import T0, battery_event, door_event, minute_samples, network_event, temp_event

from coldwatch import dt

SECRET = "a-long-test-signature-secret-of-32+bytes"


def test_temperature_samples_are_parsed_oldest_first():
    payload = temp_event(T0, 0, samples=minute_samples(T0, [3.0, 3.2, 3.4]))
    # DT lists samples newest first.
    assert [s["value"] for s in payload["event"]["data"]["temperature"]["samples"]] == [3.4, 3.2, 3.0]
    ev = dt.parse_payload(payload)
    assert [s.value for s in ev.samples] == [3.0, 3.2, 3.4]
    assert ev.time == pytest.approx(T0 + 120)
    assert ev.device_id == "tempsensor0000000001"
    assert ev.labels["unit"] == "Fridge"
    assert ev.device_type == "temperature"


def test_door_battery_and_network_events():
    assert dt.parse_payload(door_event(T0, True)).door_open is True
    assert dt.parse_payload(door_event(T0, False)).door_open is False
    battery = dt.parse_payload(battery_event(T0, 8, "LOW"))
    assert (battery.battery_pct, battery.battery_state) == (8, "LOW")
    assert dt.parse_payload(network_event(T0)).signal_strength == 80


def test_unknown_event_types_are_parsed_but_not_supported():
    payload = dt.make_payload(project_id="p", device_id="d", event_type="touch", data={"updateTime": dt.format_time(T0)},
                              timestamp=T0)
    ev = dt.parse_payload(payload)
    assert ev.event_type == "touch" and not ev.supported


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event": {"eventId": "x", "eventType": "temperature", "targetName": "nope"}},
        {"event": {"eventId": "x", "eventType": "temperature", "targetName": "projects/p/devices/d", "data": {}}},
        {"event": {"eventId": "x", "eventType": "objectPresent", "targetName": "projects/p/devices/d",
                   "data": {"objectPresent": {"state": "MAYBE"}}}},
    ],
)
def test_malformed_payloads_raise(payload):
    with pytest.raises(dt.PayloadError):
        dt.parse_payload(payload)


def test_parse_time_handles_nanoseconds_and_offsets():
    expected = datetime(2026, 10, 30, 12, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
    assert dt.parse_time("2026-10-30T12:00:00.123456789Z") == pytest.approx(expected)
    assert dt.parse_time("2026-10-30T13:00:00.123456+01:00") == pytest.approx(expected)
    assert dt.parse_time(dt.format_time(expected)) == pytest.approx(expected)


def test_valid_signature_is_accepted():
    body = dt.encode_body(temp_event(T0, 3.0))
    claims = dt.verify_signature(body, dt.sign_body(body, SECRET), SECRET)
    assert claims["checksum_sha256"] == hashlib.sha256(body).hexdigest()


def test_missing_signature_is_400():
    with pytest.raises(dt.SignatureError) as err:
        dt.verify_signature(b"{}", None, SECRET)
    assert err.value.status == 400


def test_tampered_body_is_401():
    body = dt.encode_body(temp_event(T0, 3.0))
    token = dt.sign_body(body, SECRET)
    with pytest.raises(dt.SignatureError) as err:
        dt.verify_signature(body.replace(b"3.0", b"2.0"), token, SECRET)
    assert err.value.status == 401


def test_wrong_secret_or_algorithm_is_401():
    body = b'{"event":{}}'
    with pytest.raises(dt.SignatureError):
        dt.verify_signature(body, dt.sign_body(body, "another-secret-that-is-also-long-enough"), SECRET)
    unsigned = jwt.encode({"checksum_sha256": hashlib.sha256(body).hexdigest()}, None, algorithm="none")
    with pytest.raises(dt.SignatureError):
        dt.verify_signature(body, unsigned, SECRET)
