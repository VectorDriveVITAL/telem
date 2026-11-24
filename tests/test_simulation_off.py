import pytest
from fastapi.testclient import TestClient

from app import generator
from app.config import settings
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "simulate", False)
    with TestClient(app) as c:
        yield c


def _buoy(c, name):
    return next(b for b in c.get("/api/buoys").json() if b["id"] == name)


def test_unreported_values_are_empty_and_real_ones_show(client):
    client.post("/api/buoys", json={"id": "simoff-1", "lat": 1, "lng": 2, "sensors": ["air_temp", "ph"]})
    try:
        b = _buoy(client, "simoff-1")
        assert (b["battery"], b["signal_dbm"], b["satellites"], b["solar_watts"]) == (None, None, None, None)
        assert b["sensors"]["ph"]["value"] is None and not b["sensors"]["ph"]["simulated"]
        assert b["gnss_fix"] == "No data" and b["stale_sensor_count"] == 0
        assert b["status"] == "healthy"  # nothing reported, nothing to judge

        client.post("/api/ingest/buoys/simoff-1", json={"air_temp": 21.5, "battery": 90})
        b = _buoy(client, "simoff-1")
        assert b["sensors"]["air_temp"]["value"] == 21.5 and b["battery"] == 90
        assert b["sensors"]["ph"]["value"] is None
    finally:
        client.delete("/api/buoys/simoff-1")


def test_tick_changes_nothing_and_simulate_endpoints_refuse(client):
    client.post("/api/buoys", json={"id": "simoff-2", "lat": 1, "lng": 2, "sensors": ["air_temp"]})
    client.post("/api/services", json={"name": "simoff-svc"})
    try:
        before = generator.get_buoy_state("simoff-2")["sensors"]["air_temp"]["value"]
        generator._tick_once()
        assert generator.get_buoy_state("simoff-2")["sensors"]["air_temp"]["value"] == before
        assert client.post("/api/buoys/simoff-2/simulate").status_code == 409
        assert client.post("/api/services/simoff-svc/simulate").status_code == 409
    finally:
        client.delete("/api/buoys/simoff-2")
        client.delete("/api/services/simoff-svc")
