import pytest

from app import receiver, state_store
from app.config import settings


@pytest.fixture
def rx(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "s.sqlite3"))
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "bridge.py").write_text(
        "import sys, time\nprint(' '.join(sys.argv[1:]), flush=True)\ntime.sleep(60)\n"
    )
    monkeypatch.setattr(settings, "receiver_dir", str(tmp_path))
    monkeypatch.setattr(receiver, "_mode", None)
    monkeypatch.setattr(receiver, "_proc", None)
    monkeypatch.setattr(receiver, "_log_loaded", False)
    monkeypatch.setattr(receiver, "_dry_run", False)
    monkeypatch.setattr(receiver, "_port", None)
    receiver._log.clear()
    receiver._reset_link()
    pushed = []
    monkeypatch.setattr(receiver, "_push_service", lambda **f: pushed.append(f))
    receiver.pushed = pushed
    yield tmp_path
    receiver.stop()


def test_start_stop_restart(rx):
    assert not receiver.status()["running"]
    assert receiver.start("http://127.0.0.1:8000")["running"]
    first = receiver._proc
    receiver.restart()
    assert receiver._proc is not first and receiver.running()
    assert not receiver.stop()["running"]


def test_mode_flashes_with_bridge_stopped_then_resumes(rx, monkeypatch):
    receiver.start()
    calls = []

    def fake_flash(d, mode):
        calls.append((mode, receiver.running(), receiver.status()["flashing"]))
        return True, ""

    monkeypatch.setattr(receiver, "_flash", fake_flash)
    s = receiver.set_mode("tx")
    assert calls == [("tx", False, "tx")]  # bridge down and flagged busy while flashing
    assert s["running"] and s["mode"] == "tx" and s["flashing"] is None
    assert state_store.load_receiver() == {"mode": "tx"}


def test_failed_flash_keeps_mode_and_restarts_bridge(rx, monkeypatch):
    receiver.start()
    monkeypatch.setattr(receiver, "_flash", lambda d, mode: (False, "no board"))
    with pytest.raises(ValueError, match="no board"):
        receiver.set_mode("rx")
    assert receiver.running() and receiver.status()["mode"] is None
    assert receiver.status()["flashing"] is None
    with pytest.raises(ValueError):
        receiver.set_mode("both")


def test_link_health_counts_packets_and_gaps(rx):
    receiver._link["fresh"] = 0  # past the connect flush, so gaps count
    for seq in (10, 11, 14):  # 12 and 13 never arrived
        receiver._add(f"19:00:00  board | LoRa RX #{seq}  RSSI -40 dBm  SNR 12 dB")
    link = receiver.status()["link"]
    assert (link["received"], link["lost"], link["rssi"], link["snr"]) == (3, 2, -40, 12)
    assert link["age_s"] is not None and link["age_s"] < 2
    receiver._add("19:00:05  serial lost (x) - reconnecting in 3s")
    assert receiver.status()["connected_port"] is None


def test_dry_run_and_port_reach_the_bridge_and_port_is_validated(rx, monkeypatch):
    monkeypatch.setattr(receiver, "ports", lambda: ["/dev/tty.usbmodem1"])
    with pytest.raises(ValueError):
        receiver.start(port="--base-url=evil")  # never reaches the command line
    receiver.start(dry_run=True, port="/dev/tty.usbmodem1")
    import time

    for _ in range(50):
        if any("--dry-run" in l for l in receiver._log):
            break
        time.sleep(0.1)
    echoed = [l for l in receiver._log if "--base-url" in l and "--dry-run" in l]
    assert echoed and "--port /dev/tty.usbmodem1" in echoed[0]
    assert receiver.status()["dry_run"] is True


def test_log_persists_across_restarts(rx):
    receiver._add("19:00:00  board | hello")
    receiver._log.clear()
    receiver._log_loaded = False
    assert "19:00:00  board   log      hello" in receiver.status()["log"]


def test_role_is_read_from_board_output(rx):
    receiver._add("19:00:00  board | LoRa TX #7: V1,7,,")
    s = receiver.status()
    assert s["mode"] == "tx" and s["link"]["tx_seq"] == 7
    receiver._add("19:00:05  board | LoRa RX #8  RSSI -40 dBm  SNR 12 dB")
    assert receiver.status()["mode"] == "rx"


@pytest.mark.parametrize(
    "raw, canon",
    [
        ("10:24:09  connected to /dev/cu.usbmodem1102 @ 115200 -> http://x",
         "10:24:09  bridge  connect  /dev/cu.usbmodem1102 @ 115200 -> http://x"),
        ("10:24:10  board | LoRa RX #11  RSSI -18 dBm  SNR 12 dB",
         "10:24:10  board   rx       #11  RSSI -18 dBm  SNR 12 dB"),
        ("19:41:18  board | LoRa TX #5: V1,5,,,-4.74",
         "19:41:18  board   tx       #5  V1,5,,,-4.74"),
        ("10:24:22  board | ... listening, 3 packets so far",
         "10:24:22  board   idle     listening, 3 packets so far"),
        ("19:41:23  board | water  NTC        -4.73 C    43952 ohm",
         "19:41:23  board   sensor   water  NTC        -4.73 C    43952 ohm"),
        ("19:41:18  board | awake 190 ms (duty 3.8 %), sleeping 4810 ms",
         "19:41:18  board   power    awake 190 ms (duty 3.8 %), sleeping 4810 ms"),
        ("19:41:18  board | rak> OK", "19:41:18  board   log      rak> OK"),
        ('10:37:07  board | 10.44,"core_humidity":87.63,"ph":7.53}',
         '10:37:07  board   partial  10.44,"core_humidity":87.63,"ph":7.53}'),
        ("10:36:22  [telem] bridge stopped", "10:36:22  telem   stop     bridge stopped"),
        ("10:24:10  sent | 10.51 ppm  (HTTP 200)", "10:24:10  bridge  post     10.51 ppm  (HTTP 200)"),
        ('10:25:40  DRY  | POST {"a": 1}', '10:25:40  bridge  dry      POST {"a": 1}'),
        ("10:25:41  FAIL | HTTP 500: x - spooled", "10:25:41  bridge  fail     HTTP 500: x - spooled"),
        ("10:25:41  serial lost (x) - reconnecting in 3s",
         "10:25:41  bridge  serial   serial lost (x) - reconnecting in 3s"),
        ("10:25:41  flash | Download verified successfully",
         "10:25:41  flash   out      Download verified successfully"),
    ],
)
def test_lines_are_normalised_to_source_and_action(raw, canon):
    assert receiver._canon(raw) == canon
    assert receiver._canon(canon) == canon  # idempotent, so old and new log files mix safely


def test_canonical_lines_still_drive_link_health(rx):
    receiver._link["fresh"] = 0
    receiver._add("10:00:00  board | LoRa RX #1  RSSI -20 dBm  SNR 9 dB")
    receiver._add("10:00:05  board | LoRa RX #3  RSSI -21 dBm  SNR 9 dB")
    assert receiver.status()["log"][-1] == "10:00:05  board   rx       #3  RSSI -21 dBm  SNR 9 dB"
    assert receiver.status()["link"]["lost"] == 1


def test_seq_jump_right_after_connecting_is_not_counted_as_loss(rx):
    receiver._add("10:00:00  connected to /dev/x @ 115200")
    for seq in (159, 160, 167, 168, 169, 172):  # 160 -> 167 is the connect flush, 169 -> 172 is real
        receiver._add(f"10:00:00  board | LoRa RX #{seq}  RSSI -18 dBm  SNR 12 dB")
    assert receiver.status()["link"]["lost"] == 2


def test_link_health_is_reported_as_a_service(rx, monkeypatch):
    times = iter([100.0, 105.0, 110.0, 125.0])
    monkeypatch.setattr(receiver.time, "time", lambda: next(times))
    receiver._link["fresh"] = 0
    for seq in (1, 2, 3, 5):  # #4 never arrived
        receiver._add(f"10:00:00  board | LoRa RX #{seq}  RSSI -18 dBm  SNR 12 dB")
    first, second, third, last = receiver.pushed
    assert first == {"error_rate": 0.0}  # nothing to compare the first packet with yet
    assert second == {"error_rate": 0.0, "latency_ms": 5000}  # one settled arrival: no rate yet
    assert third["rps"] == pytest.approx(0.2)  # 2 arrivals 5 s apart
    assert last["latency_ms"] == 15000 and last["error_rate"] == pytest.approx(20.0)  # 1 lost in 5


def test_connect_flush_is_not_reported_as_latency(rx):
    receiver._add("10:00:00  connected to /dev/x @ 115200")  # fresh: first packets arrive in a burst
    receiver._add("10:00:00  board | LoRa RX #1  RSSI -18 dBm  SNR 12 dB")
    receiver._add("10:00:01  board | LoRa RX #2  RSSI -18 dBm  SNR 12 dB")
    assert all("latency_ms" not in f for f in receiver.pushed)


def test_health_failure_never_breaks_the_log(rx, monkeypatch):
    def boom(**f):
        raise RuntimeError("influx down")

    monkeypatch.setattr(receiver, "_push_service", boom)
    receiver._add("10:00:00  board | LoRa RX #1  RSSI -18 dBm  SNR 12 dB")
    assert receiver.status()["link"]["received"] == 1
