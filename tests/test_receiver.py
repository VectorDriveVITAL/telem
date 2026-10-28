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
    receiver._packets.clear()
    monkeypatch.setattr(receiver, "_packets_loaded", False)
    monkeypatch.setattr(receiver, "_added", 0)
    monkeypatch.setattr(receiver, "_prefs_loaded", False)
    monkeypatch.setattr(receiver, "_keep", False)
    monkeypatch.setattr(receiver, "_backoff", 0.0)
    monkeypatch.setattr(receiver, "_next_restart", 0.0)
    monkeypatch.setattr(receiver, "_alerts", {"link": True, "hull": True, "stuck": False})
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
    assert state_store.load_receiver()["mode"] == "tx"


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
    receiver._load_packets()  # loading reads the clock; do it before the clock is faked
    receiver._load_log()
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


def test_clear_removes_the_saved_log_too(rx):
    receiver._add("19:00:00  board | hello")
    assert receiver._logfile().read_text()
    receiver.clear_log()
    assert receiver._logfile().read_text() == ""
    receiver._log_loaded = False  # a fresh run reloads from the file
    assert receiver.status()["log"] == []


def _pkt(t, seq, rssi=-18, snr=12, lost=0, fresh=False, restart=False):
    receiver._record_packet(
        {"t": t, "seq": seq, "rssi": rssi, "snr": snr, "lost": lost, "fresh": fresh, "restart": restart}
    )


def test_stats_delivery_ratio_jitter_and_events(rx):
    now = 100_000.0
    _pkt(now - 40, 1, fresh=True)  # connect burst: not an interval
    _pkt(now - 35, 2)
    _pkt(now - 30, 3)
    _pkt(now - 20, 5, lost=1)  # 4 never arrived
    _pkt(now - 10, 1, restart=True)  # the buoy rebooted
    _pkt(now - 3 * 3600, 90, rssi=-30)  # outside 1 h, inside 24 h
    s = receiver.stats(now)
    h1, d1 = s["windows"]["1h"], s["windows"]["24h"]
    assert (h1["received"], h1["lost"], h1["restarts"]) == (5, 1, 1)
    assert h1["pdr"] == pytest.approx(100 * 5 / 6, abs=0.01)
    # 35->30 (5 s) and 30->20 (10 s, a packet was lost); the connect burst and the restart are excluded
    assert h1["interval_s"] == {"mean": 7.5, "jitter": 2.5, "max": 10.0}
    assert h1["events"] == [
        {"t": now - 20, "lost": 1, "from": 4, "to": 4, "restart": False},
        {"t": now - 10, "lost": 0, "from": 1, "to": 0, "restart": True},
    ]
    assert d1["received"] == 6 and d1["rssi"]["min"] == -30


def test_stats_link_margin_and_range_estimate(rx):
    _pkt(100.0, 20, rssi=-18, snr=12)
    c = receiver.stats(101.0)["current"]
    assert c["snr_margin_db"] == pytest.approx(19.5) and c["rssi_margin_db"] == 105
    assert 0.5 < c["range_m_estimate"] < 2  # 32 dB of path loss at 915 MHz is about a metre
    assert c["buoy_uptime_s_estimate"] == 100 and c["age_s"] == 1.0
    _pkt(110.0, 21, rssi=-120)  # a weak link reads as far
    assert receiver.stats(111.0)["current"]["range_m_estimate"] > 1000


def test_histogram_bins_are_contiguous():
    assert receiver._histogram([-18, -17, -35], 10) == {"lo": -40, "width": 10, "counts": [1, 0, 2]}
    assert receiver._histogram([], 3)["counts"] == []


def test_pipeline_counters_come_from_bridge_output(rx):
    receiver._add("10:00:00  sent | 10.5 ppm  (HTTP 200, 14 ms)")
    receiver._add("10:00:05  sent | 10.6 ppm  (HTTP 200, 30 ms)")
    receiver._add("10:00:10  sent | 10.7 ppm  (HTTP 200)")  # an older bridge: no latency
    receiver._add("10:00:15  FAIL | HTTP 500: x - spooled")
    receiver._add('10:00:20  DRY  | POST {"a": 1}')
    (rx / "tools" / "spool.ndjson").write_text('{"a":1}\n\n{"b":2}\n')
    p = receiver.stats()["pipeline"]
    assert (p["posted"], p["failed"], p["dry"], p["spool_backlog"]) == (3, 1, 1, 2)
    assert p["post_ms"] == {"avg": 22, "p95": 30, "n": 2}


def test_packet_history_survives_a_restart(rx):
    receiver._add("10:00:00  board | LoRa RX #7  RSSI -20 dBm  SNR 9 dB")
    receiver._packets.clear()
    receiver._packets_loaded = False
    assert [r["seq"] for r in receiver._packets_after_load()] == [7]


def test_status_returns_only_new_lines_since_a_cursor(rx):
    for i in range(3):
        receiver._add(f"10:00:0{i}  board | rak> {i}")
    s = receiver.status()
    assert len(s["log"]) == 3 and s["log_total"] == 3
    receiver._add("10:00:09  board | rak> new")
    s2 = receiver.status(since=3)
    assert [l[-3:] for l in s2["log"]] == ["new"] and s2["log_total"] == 4
    assert receiver.status(since=4)["log"] == []
    epoch = s["log_epoch"]
    assert receiver.clear_log()["log_epoch"] != epoch


# ---- keep running, restart on crash, orphans ---------------------------------------------------


def test_start_means_keep_running_and_stop_means_stay_stopped(rx):
    receiver.start()
    assert state_store.load_receiver()["keep"] is True
    receiver.shutdown()  # telem exiting must not forget that the bridge should be on
    assert state_store.load_receiver()["keep"] is True and not receiver.running()
    receiver._stop_watch.clear()
    receiver.start()
    receiver.stop()
    assert state_store.load_receiver()["keep"] is False


def test_watchdog_restarts_a_bridge_that_died_and_backs_off(rx):
    receiver.start()
    receiver._proc.kill()
    receiver._proc.wait()
    receiver._watch_bridge(1000.0)
    assert receiver.running()
    assert any("exited unexpectedly" in l for l in receiver._log)
    receiver._proc.kill()
    receiver._proc.wait()
    receiver._watch_bridge(1001.0)  # inside the backoff window
    assert not receiver.running()
    receiver._watch_bridge(1000.0 + 7)
    assert receiver.running()


def test_watchdog_leaves_a_stopped_bridge_alone(rx):
    receiver.start()
    receiver.stop()
    receiver._watch_bridge(1000.0)
    assert not receiver.running()


def test_autostart_brings_back_a_bridge_left_on(rx):
    state_store.save_receiver({"keep": True, "dry_run": True, "mode": "rx"})
    receiver.start_background()
    try:
        assert receiver.running() and receiver.status()["dry_run"] is True
    finally:
        receiver.shutdown()


def test_orphaned_bridge_is_stopped_before_a_new_one_starts(rx):
    import subprocess as sp
    import sys

    orphan = sp.Popen([sys.executable, str(rx / "tools" / "bridge.py")])
    receiver._pidfile().parent.mkdir(parents=True, exist_ok=True)
    receiver._pidfile().write_text(str(orphan.pid))
    receiver.reap_orphan()
    assert orphan.wait(5) is not None  # it was terminated
    assert not receiver._pidfile().exists()


def test_reaping_ignores_an_unrelated_process_with_a_recycled_pid(rx):
    import subprocess as sp

    other = sp.Popen(["sleep", "30"])
    try:
        receiver._pidfile().parent.mkdir(parents=True, exist_ok=True)
        receiver._pidfile().write_text(str(other.pid))
        receiver.reap_orphan()
        assert other.poll() is None  # still alive: not ours
    finally:
        other.kill()


# ---- stats additions ---------------------------------------------------------------------------


def test_timeline_buckets_lost_and_received_per_half_hour(rx):
    now = 100_000.0
    _pkt(now - 100, 5)
    _pkt(now - 90, 7, lost=1)
    _pkt(now - 3600, 1)
    tl = receiver.stats(now)["timeline"]
    assert len(tl) == 48
    assert tl[-1]["received"] == 2 and tl[-1]["lost"] == 1
    assert sum(b["received"] for b in tl) == 3


def test_pipeline_reconciles_received_against_posted(rx):
    for seq in (1, 2, 3, 4):
        receiver._add(f"10:00:0{seq}  board | LoRa RX #{seq}  RSSI -18 dBm  SNR 12 dB")
    receiver._add("10:00:01  sent | 1 ppm  (HTTP 200, 9 ms)")
    receiver._add("10:00:02  FAIL | HTTP 500 - spooled")
    receiver._add("10:00:03  skip | nothing publishable")
    p = receiver.stats()["pipeline"]
    assert (p["posted"], p["failed"], p["skipped"], p["unaccounted"], p["success_pct"]) == (1, 1, 1, 1, 50.0)


def test_tx_counters_and_duty_cycle(rx):
    for n in (1, 2):
        receiver._add(f"10:00:0{n}  board | LoRa TX #{n}: V1,{n},,")
    receiver._add("10:00:03  board | LoRa send FAILED")
    receiver._add("10:00:04  board | awake 190 ms (duty 3.8 %), sleeping 4810 ms")
    link = receiver.status()["link"]
    assert (link["tx_sent"], link["tx_failed"], link["awake_ms"], link["duty_pct"]) == (2, 1, 190, 3.8)


def test_packets_lookup_and_exports(rx):
    _pkt(1000.0, 5)
    _pkt(1005.0, 6)
    _pkt(1010.0, 5)  # the buoy restarted and counted up to 5 again
    assert [r["t"] for r in receiver.packets(seq=5)] == [1010.0, 1000.0]
    csv = receiver.export_packets_csv().splitlines()
    assert csv[0].startswith("time,seq,rssi_dbm") and len(csv) == 4
    receiver._add("10:00:00  board | rak> hi")
    assert receiver.export_log_text().endswith("rak> hi\n")


def test_alert_choices_are_validated_and_saved(rx):
    assert receiver.set_alerts(stuck=True, hull=False) == {"link": True, "hull": False, "stuck": True}
    assert state_store.load_receiver()["alerts"]["stuck"] is True
    with pytest.raises(ValueError):
        receiver.set_alerts(nonsense=True)
