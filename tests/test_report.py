import re

from helpers import T0
from test_maintenance import run_backfill

from coldwatch.report import build_report


def test_report_lists_days_excursions_and_corrective_actions():
    engine = run_backfill("power-outage", days=7)
    [excursion] = [dict(a) for a in engine.db.all("SELECT * FROM alerts WHERE kind='temp_high'")]
    engine.acknowledge(excursion["id"], "Ola Nordmann", "Discarded dairy above 8 °C, called refrigeration service")
    html = build_report(engine.db, engine.settings, days=7, now=T0)

    assert "Temperature log (HACCP)" in html
    assert "Walk-in fridge" in html and "Freezer" in html
    assert html.count("<tr><td>20") >= 14  # 7 daily rows per unit
    assert "Temperature above limit" in html
    assert "Ola Nordmann" in html and "Discarded dairy above 8 °C" in html
    assert "Checked by (name)" in html


def test_report_shows_sensor_downtime():
    engine = run_backfill("sensor-offline", days=7)
    html = build_report(engine.db, engine.settings, days=7, now=T0)
    freezer = html[html.index("<h2>Freezer</h2>"): html.index("<h2>Walk-in fridge</h2>")]
    uptimes = [float(u) for u in re.findall(r"([\d.]+) %", freezer)]
    assert min(uptimes) < 95 and max(uptimes) == 100.0
    assert "Sensor offline" in html


def test_report_escapes_user_text():
    engine = run_backfill("power-outage", days=2)
    alert_id = engine.db.one("SELECT id FROM alerts")["id"]
    engine.acknowledge(alert_id, "<script>x</script>", "ok")
    html = build_report(engine.db, engine.settings, days=2, now=T0)
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
