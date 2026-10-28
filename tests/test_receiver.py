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
    assert "19:00:00  board | hello" in receiver.status()["log"]


def test_role_is_read_from_board_output(rx):
    receiver._add("19:00:00  board | LoRa TX #7: V1,7,,")
    s = receiver.status()
    assert s["mode"] == "tx" and s["link"]["tx_seq"] == 7
    receiver._add("19:00:05  board | LoRa RX #8  RSSI -40 dBm  SNR 12 dB")
    assert receiver.status()["mode"] == "rx"
