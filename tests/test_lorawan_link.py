import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import lorawan
from app.main import app

spec = importlib.util.spec_from_file_location("lorawan_adapter", Path(__file__).parent.parent / "pi" / "lorawan_adapter.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean():
    lorawan.reset()
    yield
    lorawan.reset()


def feed(counts, step=60):
    for i, n in enumerate(counts):
        lorawan.record("ac1f09fffe1d7fad", "buoy-07", n, -30, 13.8, T0 + timedelta(seconds=i * step))


def one(now):
    return lorawan.status(now)["devices"][0]


def test_gaps_in_the_frame_counter_are_lost_packets():
    feed([0, 1, 4, 5])  # 2 and 3 never arrived
    d = one(T0 + timedelta(seconds=200))
    assert (d["uplinks"], d["lost"], d["restarts"], d["delivery"]) == (4, 2, 0, 66.7)
    assert [e["note"] for e in lorawan.status()["events"]][::-1] == ["", "", "2 lost", ""]


def test_a_counter_that_goes_back_is_a_restart_not_a_loss():
    feed([5, 6, 0, 1])
    d = one(T0 + timedelta(seconds=200))
    assert (d["lost"], d["restarts"]) == (0, 1)


def test_silent_after_three_typical_gaps():
    feed([0, 1, 2, 3])  # a packet a minute
    assert not one(T0 + timedelta(seconds=180 + 100))["silent"]
    assert one(T0 + timedelta(seconds=180 + 200))["silent"]


def test_a_single_uplink_is_never_called_silent():
    feed([0])  # no interval known yet
    assert not one(T0 + timedelta(hours=5))["silent"]


def test_endpoints_round_trip():
    with TestClient(app) as c:
        r = c.post("/api/lorawan/uplink", json={"dev_eui": "AC1F09FFFE1D7FAD", "buoy": None, "f_cnt": 3, "rssi": -80, "snr": 9.5})
        assert r.status_code == 200
        body = c.get("/api/lorawan").json()
        assert body["devices"][0]["dev_eui"] == "ac1f09fffe1d7fad" and body["devices"][0]["buoy"] is None
        assert body["events"][0]["f_cnt"] == 3
        assert c.post("/api/lorawan/uplink", json={"dev_eui": "", "f_cnt": -1}).status_code == 422


UPLINK = {"deviceInfo": {"devEui": "AC1F09FFFE1D7FAD"}, "fPort": 1, "fCnt": 7, "time": "2026-11-20T00:00:00+00:00",
          "rxInfo": [{"rssi": -30, "snr": 13.8}], "data": "AQUEyAjUElc="}


def test_adapter_reports_link_for_known_and_unknown_devices_only_on_the_readings_port():
    got = []
    a.handle(json.dumps(UPLINK), {"ac1f09fffe1d7fad": "buoy-07"}, lambda b, body: None, link=got.append)
    a.handle(json.dumps(UPLINK), {}, lambda b, body: None, set(), got.append)
    a.handle(json.dumps({**UPLINK, "fPort": 9}), {}, lambda b, body: None, link=got.append)
    assert got == [{"dev_eui": "ac1f09fffe1d7fad", "buoy": b, "f_cnt": 7, "rssi": -30, "snr": 13.8} for b in ("buoy-07", None)]


def test_post_link_survives_telem_being_down():
    a.post_link("http://127.0.0.1:1", {"dev_eui": "x"})  # must not raise
