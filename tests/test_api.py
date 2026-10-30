import time

import pytest
from fastapi.testclient import TestClient
from helpers import minute_samples, temp_event

from coldwatch import dt
from coldwatch.app import create_app
from coldwatch.config import Settings

SECRET = "api-test-signature-secret-0123456789abcdef"


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.db"), signature_secret=SECRET)
    with TestClient(create_app(settings, background=False)) as c:
        yield c


def post(client, payload, *, secret=SECRET, body=None):
    raw = body if body is not None else dt.encode_body(payload)
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Dt-Signature"] = dt.sign_body(raw, secret)
    return client.post("/dt/events", content=raw, headers=headers)


def test_signed_event_is_stored_and_duplicate_acknowledged(client):
    payload = temp_event(time.time(), 3.1)
    r = post(client, payload)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert post(client, payload).json() == {"status": "duplicate"}
    [unit] = client.get("/api/units").json()
    assert unit["name"] == "Fridge" and unit["temperature"] == pytest.approx(3.1)


def test_unsigned_request_is_rejected(client):
    assert post(client, temp_event(time.time(), 3.0), secret=None).status_code == 400


def test_tampered_request_is_rejected(client):
    raw = dt.encode_body(temp_event(time.time(), 3.0))
    token = dt.sign_body(raw, SECRET)
    r = client.post("/dt/events", content=raw.replace(b"3.0", b"9.0"),
                    headers={"Content-Type": "application/json", "x-dt-signature": token})
    assert r.status_code == 401


def test_garbage_is_400(client):
    assert post(client, None, body=b"not json").status_code == 400
    assert post(client, None, body=b'{"hello": "world"}').status_code == 400


def test_alert_flow_through_the_api(client):
    now = time.time()
    post(client, temp_event(0, 0, samples=minute_samples(now - 1200, [8.0] * 21)))
    [alert] = client.get("/api/alerts").json()
    assert alert["kind"] == "temp_high" and alert["unit_name"] == "Fridge" and alert["level"] == 1

    r = client.post(f"/api/alerts/{alert['id']}/ack", json={"name": "Kari", "action": "Moved stock"})
    assert r.status_code == 200 and r.json()["acked_by"] == "Kari"
    assert client.post("/api/alerts/999/ack", json={"name": "x"}).status_code == 404

    readings = client.get("/api/readings", params={"unit": "fridge", "hours": 1}).json()
    assert readings["max_temp"] == 5.0 and len(readings["points"]) >= 15
    assert client.get("/api/readings", params={"unit": "nope"}).status_code == 404


def test_pages_render(client):
    post(client, temp_event(time.time(), 3.0))
    assert "ColdWatch" in client.get("/").text
    report = client.get("/report", params={"days": 3})
    assert report.status_code == 200 and "Fridge" in report.text
    assert client.get("/healthz").json() == {"ok": True}
