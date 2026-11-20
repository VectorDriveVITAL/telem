import pytest
from fastapi.testclient import TestClient

from app import generator as fleet
from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "seed_demo", False)
    monkeypatch.setattr(fleet, "_state", {"services": {}, "buoys": {}, "alerts": [], "incidents": {}, "maintenance": [], "mutes": {}})
    monkeypatch.setattr(fleet, "write_points", lambda points: None)
    fleet.seed_history()
    with TestClient(app) as c:
        yield c


def units(client):
    buoy = next(b for b in client.get("/api/buoys").json() if b["id"] == "b1")
    return {k: v["unit"] for k, v in buoy["sensors"].items()}


def test_core_sensors_get_units_without_the_core_sending_any(client):
    names = ["air_temp", "humidity", "core_temp", "core_humidity", "pressure", "total_dissolved_solids", "conductivity"]
    client.post("/api/buoys", json={"id": "b1", "lat": 0, "lng": 0, "sensors": names})
    assert units(client) == dict(zip(names, ["°C", "%", "°C", "%", "hPa", "ppm", "µS/cm"]))


def test_a_sensor_registered_before_the_defaults_existed_is_fixed_by_its_next_reading(client):
    client.post("/api/buoys", json={"id": "b1", "lat": 0, "lng": 0, "sensors": ["pressure"], "units": {"pressure": ""}})
    assert units(client)["pressure"] == ""
    assert client.post("/api/ingest/buoys/b1", json={"pressure": 1010.8, "mystery": 1}).status_code == 200
    assert units(client) == {"pressure": "hPa", "mystery": ""}


def test_a_unit_chosen_at_registration_is_kept(client):
    client.post("/api/buoys", json={"id": "b1", "lat": 0, "lng": 0, "sensors": ["pressure"], "units": {"pressure": "kPa"}})
    client.post("/api/ingest/buoys/b1", json={"pressure": 101.0})
    assert units(client)["pressure"] == "kPa"
