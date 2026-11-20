import base64
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import generator as fleet
from app.config import settings
from app.main import app

spec = importlib.util.spec_from_file_location("lorawan_adapter", Path(__file__).parent.parent / "pi" / "lorawan_adapter.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

# The byte layout is a contract with the firmware: these vectors are in pi/LORAWAN.md too.
V_SMALL = "010504ca08d41257"
V_FULL = "01ff07c008de081e1ea208ac1c78276800d100ee02000057"
V_NEGATIVE = "010100f3fd"


def test_decode_matches_the_published_vectors():
    assert a.decode(bytes.fromhex(V_SMALL)) == {"temp": 22.5, "humidity": 48.2, "battery": 87}
    full = a.decode(bytes.fromhex(V_FULL))
    assert full == {"temp": 22.4, "air_temp": 22.7, "humidity": 77.1, "core_temp": 22.1, "core_humidity": 73.4,
                    "pressure": 1010.4, "total_dissolved_solids": 10.4, "conductivity": 20.9, "ph": 7.5,
                    "turbidity": 0.0, "battery": 87}
    assert a.decode(bytes.fromhex(V_NEGATIVE)) == {"temp": -5.25}


def test_gnss_position_vector():
    raw = bytes.fromhex("0100e0ab14bd1114b927c709")  # shared with the core firmware
    assert a.decode(raw) == pytest.approx({"lat": 29.7604267, "lng": -95.3698028, "satellites": 9})


def test_missing_fields_stay_missing_and_zero_stays_a_reading():
    assert "ph" not in a.decode(bytes.fromhex(V_SMALL))
    assert a.decode(a.encode(turbidity=0.0))["turbidity"] == 0.0  # off is absent, zero is a value


def test_every_field_round_trips_and_the_largest_payload_fits_everywhere_above_dr0():
    values = {name: 1.0 for name, _, _ in a.FIELDS}
    assert a.decode(a.encode(**values)) == values
    assert len(a.encode(**values)) == 37  # US915 allows 53 bytes at DR1 and above


@pytest.mark.parametrize(
    "raw",
    [b"", b"\x01\x01", bytes.fromhex("0205 04 ca08 d412 57".replace(" ", "")),  # wrong version
     bytes.fromhex("0105 04 ca08"), bytes.fromhex(V_SMALL) + b"\x00",  # truncated, trailing byte
     ],
)
def test_malformed_payloads_are_refused_not_guessed(raw):
    with pytest.raises(ValueError):
        a.decode(raw)


CHIRPSTACK = {
    "deviceInfo": {"devEui": "70B3D57ED0012345", "deviceName": "pack-2"}, "fPort": 1, "fCnt": 12,
    "time": "2026-11-19T15:25:40.123456+00:00", "data": base64.b64encode(bytes.fromhex(V_SMALL)).decode(),
    "rxInfo": [{"gatewayId": "far", "rssi": -110, "snr": -2.0}, {"gatewayId": "near", "rssi": -80, "snr": 9.5}],
}
TTN = {
    "end_device_ids": {"device_id": "pack-2", "dev_eui": "70B3D57ED0012345"},
    "received_at": "2026-11-19T15:25:40.123456789Z",
    "uplink_message": {"f_port": 1, "f_cnt": 12, "frm_payload": base64.b64encode(bytes.fromhex(V_SMALL)).decode(),
                       "rx_metadata": [{"rssi": -90, "snr": 5.0}, {"rssi": -75, "snr": 11.0}]},
}


@pytest.mark.parametrize("msg, rssi, snr", [(CHIRPSTACK, -80, 9.5), (TTN, -75, 11.0)])
def test_both_network_servers_give_the_same_uplink(msg, rssi, snr):
    up = a.parse_uplink(msg)
    assert up["dev_eui"] == "70b3d57ed0012345" and up["f_port"] == 1 and up["f_cnt"] == 12
    assert up["data"] == bytes.fromhex(V_SMALL)
    assert (up["rssi"], up["snr"]) == (rssi, snr)  # the gateway that heard it best
    with pytest.raises(ValueError):
        a.parse_uplink({"something": "else"})


def test_body_has_telem_names_signal_and_a_time_telem_accepts():
    body = a.build_body(a.parse_uplink(TTN), a.decode(bytes.fromhex(V_SMALL)))
    assert body == {"temp": 22.5, "humidity": 48.2, "battery": 87, "signal_dbm": -75,
                    "time": "2026-11-19T15:25:40.123456+00:00"}  # 9 fractional digits and Z were normalised


def test_a_buffered_reading_is_dated_by_when_it_was_measured():
    up = a.parse_uplink(CHIRPSTACK)
    body = a.build_body(up, {"temp": 20.0, "age_s": 90})
    assert body["time"] == "2026-11-19T15:24:10.123456+00:00" and "age_s" not in body


def test_handle_routes_known_devices_and_reports_unknown_ones_once():
    sent, seen = [], set()
    devices = {"70b3d57ed0012345": "buoy-02"}
    line = a.handle(json.dumps(CHIRPSTACK), devices, lambda b, body: sent.append((b, body)), seen)
    assert sent[0][0] == "buoy-02" and "fCnt 12" in line and "3 fields" in line
    other = {**CHIRPSTACK, "deviceInfo": {"devEui": "ffffffffffffffff"}}
    assert "unknown device ffffffffffffffff" in a.handle(json.dumps(other), devices, sent.append, seen)
    assert a.handle(json.dumps(other), devices, sent.append, seen) == ""  # not repeated every uplink
    assert len(sent) == 1
    other_port = {**CHIRPSTACK, "fPort": 9}
    assert a.handle(json.dumps(other_port), devices, sent.append, seen).startswith("ignored")
    with pytest.raises(ValueError):
        a.handle("not json", devices, sent.append, seen)


# ---- sending, retrying and spooling against a stand-in for telem -------------------------------


class FakeTelem:
    def __init__(self, status):
        self.status, self.received = status, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def telem():
    t = FakeTelem(200)
    yield t
    t.close()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "RETRIES", 2)


def test_a_good_post_lands_on_the_buoy_route(telem):
    a.Sender(telem.url, "unused.ndjson")("buoy-02", {"temp": 21.0})
    assert telem.received == [("/api/ingest/buoys/buoy-02", {"temp": 21.0})]


def test_a_rejected_reading_is_dropped_not_retried_forever(telem, tmp_path):
    telem.status = 404  # unknown buoy: registering it later would not make this old reading right
    spool = tmp_path / "spool.ndjson"
    a.Sender(telem.url, spool)("nope", {"temp": 1.0})
    assert len(telem.received) == 1 and not spool.exists()


def test_a_down_telem_spools_and_the_spool_replays_in_order(telem, tmp_path):
    spool = tmp_path / "spool.ndjson"
    send = a.Sender(telem.url, spool)
    telem.status = 503
    send("buoy-02", {"temp": 1.0})
    send("buoy-02", {"temp": 2.0})
    assert len(spool.read_text().splitlines()) == 2
    send.replay()  # still down: nothing is lost or reordered
    assert [json.loads(l)["body"]["temp"] for l in spool.read_text().splitlines()] == [1.0, 2.0]
    telem.status, telem.received = 200, []
    send.replay()
    assert [b["temp"] for _, b in telem.received] == [1.0, 2.0] and spool.read_text() == ""


def test_an_unreachable_telem_also_spools(tmp_path):
    spool = tmp_path / "spool.ndjson"
    a.Sender("http://127.0.0.1:9", spool)("buoy-02", {"temp": 1.0})  # nothing listens on port 9
    assert spool.exists() and len(spool.read_text().splitlines()) == 1


# ---- the body is what telem actually accepts ----------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "seed_demo", False)
    monkeypatch.setattr(fleet, "_state", {"services": {}, "buoys": {}, "alerts": [], "incidents": {}, "maintenance": [], "mutes": {}})
    monkeypatch.setattr(fleet, "write_points", lambda points: None)
    fleet.seed_history()
    with TestClient(app) as c:
        c.post("/api/buoys", json={"id": "buoy-02", "lat": 29.7, "lng": -95.4, "sensors": ["temp"]})
        yield c


def test_a_decoded_uplink_is_accepted_by_telem_and_becomes_real_sensor_data(client):
    def send(buoy, body):
        r = client.post(f"/api/ingest/buoys/{buoy}", json=body)
        assert r.status_code == 200, r.text

    msg = a.sample_uplink("70b3d57ed0012345", temp=22.5, humidity=48.2, battery=87)
    a.handle(json.dumps(msg), {"70b3d57ed0012345": "buoy-02"}, send)
    b = next(x for x in client.get("/api/buoys").json() if x["id"] == "buoy-02")
    assert b["sensors"]["temp"]["value"] == 22.5 and b["sensors"]["temp"]["simulated"] is False
    assert b["sensors"]["humidity"]["value"] == 48.2 and b["battery"] == 87.0 and b["signal_dbm"] == -80


def test_once_mode_sends_one_uplink_from_stdin_and_reports_success_or_failure(telem, tmp_path, monkeypatch, capsys):
    import io
    import sys

    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({"70B3D57ED0012345": "buoy-02"}))  # upper case in the file is fine
    uplink = json.dumps(a.sample_uplink("70b3d57ed0012345", temp=21.0, battery=80))
    argv = ["x", "--once", "--devices", str(devices), "--telem", telem.url, "--spool", str(tmp_path / "s")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO(uplink))
    assert a.main() == 0 and "buoy-02" in capsys.readouterr().out
    path, body = telem.received[0]
    assert path == "/api/ingest/buoys/buoy-02" and body["temp"] == 21.0 and body["signal_dbm"] == -80
    monkeypatch.setattr(sys, "stdin", io.StringIO(uplink.replace("70b3d57ed0012345", "ffffffffffffffff")))
    assert a.main() == 1  # an unmapped device is a failure a script can notice
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert a.main() == 1
