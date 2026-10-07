import csv
import io
from datetime import datetime, timezone

import pytest

from app import state_store, vital
from app.config import settings


def test_rows_become_upstream_csv_pair():
    rows = [
        ("b1", "2026-10-06T00:00:00+00:00", 29.7, -95.4, {"temp": 18.4}, {"temp": "°C"}),
        ("b1", "2026-10-06T00:01:00+00:00", 29.8, -95.5, {"battery": 80.0}, {"battery": "%"}),
    ]
    sensors, measurements = vital.build_csvs(rows)
    assert list(csv.DictReader(io.StringIO(sensors))) == [
        {"alias": "battery", "variablename": "battery", "units": "%", "postprocess": "False"},
        {"alias": "temp", "variablename": "temp", "units": "°C", "postprocess": "False"},
    ]
    assert measurements.splitlines() == [
        "collectiontime,Lat_deg,Lon_deg,battery,temp",
        "2026-10-06T00:00:00+00:00,29.7,-95.4,,18.4",
        "2026-10-06T00:01:00+00:00,29.8,-95.5,80.0,",
    ]


def test_forward_queues_only_when_configured(monkeypatch):
    monkeypatch.setattr(vital, "_queue", vital.queue.Queue())
    ts = datetime(2026, 10, 6, tzinfo=timezone.utc)
    vital.forward("b1", ts, 1, 2, {"temp": 1.0}, {})
    assert vital._queue.empty()
    monkeypatch.setattr(vital, "_creds", {"username": "u", "password": "p", "campaign_id": "1"})
    vital.forward("b1", ts, 1, 2, {"lat": 1.0, "lng": 2.0}, {})  # position only: nothing to send
    vital.forward("b1", ts, 1, 2, {"temp": 1.0, "lat": 1.0}, {})
    assert vital._queue.get_nowait()[4] == {"temp": 1.0}


def test_credentials_are_checked_saved_and_never_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "s.sqlite3"))
    monkeypatch.setattr(vital, "_creds", {})
    monkeypatch.setattr(vital, "_ensure_thread", lambda: None)
    monkeypatch.setattr(vital, "_login", lambda creds: "T")

    class Campaign:
        def __enter__(self):
            return io.BytesIO(b'{"name": "Bay"}')

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(vital, "urlopen", lambda req, timeout: Campaign())
    with pytest.raises(ValueError):
        vital.configure("", "u", "", 5)  # no password saved yet
    status = vital.configure("", "u", "secret", 5)
    assert status["enabled"] and status["campaign_name"] == "Bay"
    assert "secret" not in str(status)
    assert state_store.load_vital()["password"] == "secret"
    vital.configure("", "u2", "", 6)  # blank password keeps the saved one
    assert state_store.load_vital()["password"] == "secret"
    assert not vital.disconnect()["enabled"]
    assert state_store.load_vital() == {}


def test_simulated_ticks_are_forwarded(tmp_path, monkeypatch):
    from app import generator as fleet

    monkeypatch.setattr(settings, "state_db", str(tmp_path / "s.sqlite3"))
    empty = {"services": {}, "buoys": {}, "alerts": [], "incidents": {}, "maintenance": [], "mutes": {}}
    monkeypatch.setattr(fleet, "_state", empty)
    monkeypatch.setattr(fleet, "write_points", lambda points: None)
    sent = []
    monkeypatch.setattr(vital, "forward", lambda *args: sent.append(args))
    fleet.register_buoy("sim-1", 29.7, -95.4, ["temp"], {"temp": "°C"})
    fleet._tick_once()
    (buoy_id, _, lat, lng, fields, units), = sent
    assert (buoy_id, lat, lng) == ("sim-1", 29.7, -95.4)
    assert {"temp", "battery"} <= set(fields) and units["temp"] == "°C"


def test_remote_snapshot_follows_pagination(monkeypatch):
    monkeypatch.setattr(vital, "_creds", {"username": "u", "password": "p", "campaign_id": "4"})
    pages = {
        "/campaigns/4": {"id": 4, "name": "C", "stations": []},
        "/campaigns/4/stations?page=1&limit=100": {"items": [{"id": 6}], "pages": 2},
        "/campaigns/4/stations?page=2&limit=100": {"items": [{"id": 7}], "pages": 2},
        "/campaigns/4/stations/6/sensors?page=1&limit=100": {"items": [{"id": 60}], "pages": 1},
        "/campaigns/4/stations/7/sensors?page=1&limit=100": {"items": [], "pages": 1},
    }
    monkeypatch.setattr(vital, "_call", lambda method, path: pages[path])
    snap = vital.remote_snapshot()
    assert "stations" not in snap["campaign"]
    assert [(s["id"], [x["id"] for x in s["sensors"]]) for s in snap["stations"]] == [(6, [60]), (7, [])]
