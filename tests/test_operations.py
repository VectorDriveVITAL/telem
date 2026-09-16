"""Behavioral API tests; InfluxDB is replaced only for the unit suite."""

import copy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import generator as fleet
from app import state_store
from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "seed_demo", False)
    monkeypatch.setattr(
        fleet,
        "_state",
        {
            "services": {},
            "buoys": {},
            "alerts": [],
            "incidents": {},
            "maintenance": [],
            "mutes": {},
        },
    )
    monkeypatch.setattr(fleet, "write_points", lambda points: None)
    fleet.seed_history()
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def buoy(client, name="test-buoy"):
    r = client.post(
        "/api/buoys", json={"id": name, "lat": 29.7, "lng": -95.4, "sensors": ["temp"]}
    )
    assert r.status_code == 201, r.text
    return r.json()


def reading(client, **values):
    r = client.post("/api/ingest/buoys/test-buoy", json=values)
    assert r.status_code == 200, r.text
    return r.json()


def test_registration_and_deletion_survive_restart(client):
    buoy(client)
    client.post("/api/services", json={"name": "test-service"})
    saved = state_store.load()
    fleet._state = {k: {} if isinstance(v, dict) else [] for k, v in saved.items()}
    fleet.seed_history()
    assert [b["id"] for b in client.get("/api/buoys").json()] == ["test-buoy"]
    assert client.delete("/api/buoys/test-buoy").status_code == 204
    fleet.seed_history()
    assert client.get("/api/buoys").json() == []


def test_rules_custom_sensor_incident_and_recovery(client):
    buoy(client)
    reading(client, dissolved_o2=6, temp=22)
    path = "/api/buoys/test-buoy/sensors/dissolved_o2/rules"
    r = client.put(path, json={"warn_min": 5, "crit_min": 3, "stale_after_seconds": 60})
    assert r.status_code == 200
    b = reading(client, dissolved_o2=2)
    assert b["status"] == "critical"
    incidents = client.get("/api/incidents").json()
    assert incidents[0]["status"] == "ongoing"
    reading(client, dissolved_o2=6)
    assert client.get("/api/incidents").json()[0]["status"] == "resolved"
    reading(client, dissolved_o2=2)
    assert len(client.get("/api/incidents").json()) >= 2
    assert client.get(path).json()["crit_min"] == 3


def test_heartbeat_does_not_refresh_probe_or_fake_contact(client):
    buoy(client)
    old = (fleet.utcnow() - timedelta(hours=1)).isoformat()
    b = reading(client, temp=21, time=old)
    assert b["sensors"]["temp"]["stale"]
    assert (
        client.post("/api/ingest/buoys/test-buoy/heartbeat", json={}).status_code == 200
    )
    b = client.get("/api/buoys").json()[0]
    assert b["sensors"]["temp"]["last_reading_at"] == old
    assert b["sensors"]["temp"]["stale"] and not b["offline"]
    with fleet._lock:
        fleet._state["buoys"]["test-buoy"]["last_contact"] -= 1000
    fleet._tick_once()
    b = client.get("/api/buoys").json()[0]
    assert b["offline"] and b["status"] == "critical"
    assert client.get("/api/overview").json()["counts"]["offline_buoys"] == 1


def test_real_probe_not_changed_by_simulator(client):
    buoy(client)
    reading(client, temp=99, battery=25, solar_watts=2.5, signal_dbm=-88)
    fleet._tick_once()
    b = client.get("/api/buoys").json()[0]
    assert (
        b["sensors"]["temp"]["value"],
        b["battery"],
        b["solar_watts"],
        b["signal_dbm"],
    ) == (99, 25, 2.5, -88)
    assert "solar_watts" not in b["sensors"]


def test_backfill_does_not_replace_latest_reading(client):
    buoy(client)
    current = fleet.utcnow().isoformat()
    old = (fleet.utcnow() - timedelta(hours=1)).isoformat()
    reading(client, temp=22, time=current)
    b = reading(client, temp=5, time=old)
    assert b["sensors"]["temp"]["value"] == 22
    assert b["sensors"]["temp"]["last_reading_at"] == current


def test_incident_workflow_and_mute_persist(client):
    buoy(client)
    reading(client, temp=22, battery=10)
    inc = next(
        i for i in client.get("/api/incidents").json() if i["status"] == "ongoing"
    )
    path = "/api/incidents/" + inc["id"]
    assert (
        client.patch(
            path,
            json={
                "owner": "Yi Cheng",
                "acknowledged": True,
                "cause": "Probe wiring inspected",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            path + "/notes",
            json={"author": "Yi Cheng", "text": "Battery replacement scheduled"},
        ).status_code
        == 201
    )
    alert = client.get("/api/alerts").json()[0]
    assert (
        client.patch(
            "/api/alerts/" + alert["id"], json={"acknowledged": True}
        ).status_code
        == 200
    )
    mute = client.post(
        "/api/mutes",
        json={"source_type": "buoy", "source": "test-buoy", "duration_minutes": 30},
    ).json()
    fleet.seed_history()
    result = client.get(path).json()
    assert (
        result["owner"] == "Yi Cheng"
        and result["acknowledged_at"]
        and len(result["notes"]) == 1
    )
    assert client.get("/api/alerts").json()[0]["acknowledged_at"]
    assert client.get("/api/alerts").json()[0]["muted"]
    assert client.delete("/api/mutes/" + mute["id"]).status_code == 204
    assert not client.get("/api/alerts").json()[0]["muted"]
    assert client.patch(path, json={"status": "resolved"}).json()["resolved_at"]
    fleet._tick_once()
    assert client.get(path).json()["status"] == "resolved"


def test_sources_with_same_name_have_separate_incidents(client):
    buoy(client, "shared")
    client.post("/api/services", json={"name": "shared"})
    client.post("/api/ingest/services/shared", json={"latency_ms": 1000})
    client.post("/api/ingest/buoys/shared", json={"temp": 22, "battery": 10})
    active = [
        i for i in client.get("/api/incidents").json() if i["status"] == "ongoing"
    ]
    assert {i["source_type"] for i in active} == {"service", "buoy"}


def test_maintenance_and_settings_persist(client):
    buoy(client)
    r = client.post(
        "/api/buoys/test-buoy/maintenance",
        json={
            "kind": "calibration",
            "sensor": "temp",
            "description": "Two-point probe check",
            "operator": "Yi Cheng",
        },
    )
    assert r.status_code == 201
    assert (
        client.put(
            "/api/buoys/test-buoy/settings",
            json={
                "offline_after_seconds": 600,
                "mooring_lat": 29.8,
                "mooring_lng": -95.3,
            },
        ).status_code
        == 200
    )
    fleet.seed_history()
    assert (
        client.get("/api/buoys/test-buoy/maintenance").json()[0]["description"]
        == "Two-point probe check"
    )
    assert client.get("/api/buoys").json()[0]["offline_after_seconds"] == 600


@pytest.mark.parametrize(
    "payload",
    [
        {"temp": "bad"},
        {"battery": 101},
        {"lat": 100},
        {"time": "bad-date", "temp": 1},
        {"solar_watts": -2},
    ],
)
def test_invalid_ingest_is_rejected(client, payload):
    buoy(client)
    assert client.post("/api/ingest/buoys/test-buoy", json=payload).status_code == 422


def test_invalid_rules_and_batch_are_rejected_before_mutation(client):
    buoy(client)
    assert (
        client.put(
            "/api/buoys/test-buoy/sensors/temp/rules",
            json={"warn_min": 10, "warn_max": 5},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/ingest/buoys/test-buoy/batch", json=[{"temp": 20}, {"temp": "bad"}]
        ).status_code
        == 422
    )
    assert client.post("/api/ingest/buoys/test-buoy/batch", json=[]).status_code == 422
    assert (
        client.post(
            "/api/buoys/test-buoy/sensors", json={"sensor": "solar_watts"}
        ).status_code
        == 409
    )


def test_write_failure_does_not_update_current_state(client, monkeypatch):
    buoy(client)
    before = copy.deepcopy(fleet.get_buoy_state("test-buoy"))

    def fail(points):
        raise ValueError("test failure")

    monkeypatch.setattr(fleet, "write_points", fail)
    assert (
        client.post("/api/ingest/buoys/test-buoy", json={"temp": 999}).status_code
        == 400
    )
    after = fleet.get_buoy_state("test-buoy")
    assert after == before


def test_legacy_routes_and_error_cases(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/incidents/unknown").status_code == 404
    assert client.get("/api/buoys/unknown/maintenance").status_code == 404
    assert client.get("/api/buoys/unknown/timeseries").status_code == 404
    assert client.get("/api/export?kind=buoy").status_code == 400
    assert client.get("/api/export?kind=incidents").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200


def test_service_backfill_and_simulation(client):
    client.post("/api/services", json={"name": "service"})
    now = fleet.utcnow().isoformat()
    old = (fleet.utcnow() - timedelta(hours=1)).isoformat()
    client.post("/api/ingest/services/service", json={"latency_ms": 300, "time": now})
    client.post("/api/ingest/services/service", json={"latency_ms": 10, "time": old})
    assert client.get("/api/services").json()[0]["latency_ms"] == 300
    assert client.post("/api/services/service/simulate").json()["simulated"]


def test_null_sensor_value_is_ignored_for_compatibility(client):
    buoy(client)
    reading(client, temp=22)
    assert reading(client, temp=None)["sensors"]["temp"]["value"] == 22


def test_full_day_seed_covers_every_field_and_preserves_real_state(client, monkeypatch):
    buoy(client)
    reading(client, temp=22)
    client.post("/api/services", json={"name": "demo-service"})
    before = copy.deepcopy(fleet._state)
    captured = []
    monkeypatch.setattr(settings, "seed_demo", True)
    monkeypatch.setattr(settings, "history_minutes", 1440)
    monkeypatch.setattr(
        fleet,
        "make_point",
        lambda measurement, tags, fields, ts: (measurement, tags, fields, ts),
    )
    monkeypatch.setattr(fleet, "write_points", lambda points: captured.extend(points))
    fleet.seed_history()
    for measurement, expected in [
        ("service_metrics", {"latency_ms", "rps", "error_rate"}),
        ("buoy_metrics", fleet.KNOWN_BUOY_UNIVERSAL_FIELDS),
    ]:
        rows = [row for row in captured if row[0] == measurement]
        assert len(rows) == 1440
        assert rows[-1][3] - rows[0][3] == timedelta(minutes=1439)
        assert (
            timedelta(hours=24)
            <= fleet.utcnow() - rows[0][3]
            < timedelta(hours=24, minutes=1)
        )
        assert set(rows[0][2]) == expected
        for field in expected:
            assert len({row[2][field] for row in rows}) > 1
    assert fleet._state["buoys"] == before["buoys"]
    for key in ("latency", "rps", "error_rate", "last_reading_at", "simulated"):
        assert (
            fleet._state["services"]["demo-service"][key]
            == before["services"]["demo-service"][key]
        )
    captured.clear()
    fleet.seed_history()
    assert captured == []
