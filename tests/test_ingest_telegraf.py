import pytest
from fastapi.testclient import TestClient
from influxdb_client.rest import ApiException

from app import generator as fleet
from app.config import settings
from app.main import app
from app.routers.ingest import _epoch_to_iso

BUOY = "vital/buoy/test-buoy/readings"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "seed_demo", False)
    monkeypatch.setattr(
        fleet, "_state",
        {"services": {}, "buoys": {}, "alerts": [], "incidents": {}, "maintenance": [], "mutes": {}},
    )
    monkeypatch.setattr(fleet, "write_points", lambda points: None)
    fleet.seed_history()
    with TestClient(app, raise_server_exceptions=True) as c:
        c.post("/api/buoys", json={"id": "test-buoy", "lat": 29.7, "lng": -95.4, "sensors": ["temp"]})
        c.post("/api/services", json={"name": "lora-gw"})
        yield c


def metric(topic=BUOY, ts=1_900_000_000_000, **fields):
    return {"name": "mqtt_consumer", "tags": {"topic": topic}, "fields": fields, "timestamp": ts}


def sensors(client):
    return next(b for b in client.get("/api/buoys").json() if b["id"] == "test-buoy")["sensors"]


def test_batch_from_telegraf_updates_the_buoy(client):
    # a timestamp in the future is refused, so use "now" in milliseconds
    import time

    now = int(time.time() * 1000)
    r = client.post("/api/ingest/telegraf", json={"metrics": [
        metric(ts=now, temp=21.5, total_dissolved_solids=10.4),
        metric(ts=now + 1, temp=21.7, ph=7.5),
    ]})
    assert r.status_code == 200 and r.json() == {"accepted": 2, "rejected": []}
    s = sensors(client)
    assert s["temp"]["value"] == 21.7 and s["temp"]["simulated"] is False
    assert s["ph"]["value"] == 7.5 and s["total_dissolved_solids"]["value"] == 10.4  # auto-registered


def test_one_bad_metric_does_not_block_the_batch(client):
    r = client.post("/api/ingest/telegraf", json={"metrics": [
        metric("vital/buoy/nobody/readings", ts=None, temp=1.0),  # unknown buoy
        metric("sensors/test", ts=None, temp=1.0),  # not in the topic plan
        metric(ts=None, battery=150.0),  # fails telem's own validation
        metric(ts=None, note=1.0, flag=True),  # booleans are not readings; note is a fine name
        metric(ts=None),  # nothing numeric
        metric(ts=None, temp=22.0),  # the good one
    ]})
    body = r.json()
    assert r.status_code == 200 and body["accepted"] == 2
    reasons = {x["index"]: x["reason"] for x in body["rejected"]}
    assert set(reasons) == {0, 1, 2, 4}
    assert "register it first" in reasons[0] and "topic" in reasons[1] and "no numeric" in reasons[4]
    assert "battery" in reasons[2] and "100" in reasons[2]  # says which field and why
    assert sensors(client)["temp"]["value"] == 22.0


def test_single_metric_and_bare_list_are_accepted(client):
    assert client.post("/api/ingest/telegraf", json=metric(ts=None, temp=20.0)).json()["accepted"] == 1
    assert client.post("/api/ingest/telegraf", json=[metric(ts=None, temp=20.5)]).json()["accepted"] == 1
    assert client.post("/api/ingest/telegraf", json="nonsense").status_code == 400


def test_service_metrics_are_routed_by_topic(client):
    r = client.post("/api/ingest/telegraf", json=metric("vital/service/lora-gw/metrics", ts=None, latency_ms=42.0, rps=3.0, junk=9.0))
    assert r.json()["accepted"] == 1
    svc = client.get("/api/services/lora-gw/timeseries").status_code  # exists and is queryable
    assert svc == 200
    r = client.post("/api/ingest/telegraf", json=metric("vital/service/lora-gw/metrics", ts=None, junk=9.0))
    assert r.json()["rejected"][0]["reason"] == "no numeric fields"


@pytest.mark.parametrize("unit_scale", [1, 1_000, 1_000_000, 1_000_000_000])
def test_timestamp_units_all_resolve_to_the_same_instant(unit_scale):
    assert _epoch_to_iso(1_730_000_000 * unit_scale) == "2024-10-27T03:33:20+00:00"


def test_bad_or_future_timestamps_are_rejected(client):
    far = 4_000_000_000_000  # year 2096, in ms
    r = client.post("/api/ingest/telegraf", json=[metric(ts=far, temp=1.0), metric(ts="soon", temp=1.0)])
    assert r.json()["accepted"] == 0 and len(r.json()["rejected"]) == 2


def test_storage_failure_is_not_swallowed_so_telegraf_retries(client, monkeypatch):
    def down(points):
        raise ApiException(status=503)

    monkeypatch.setattr(fleet, "write_points", down)
    assert client.post("/api/ingest/telegraf", json=metric(ts=None, temp=1.0)).status_code == 503
