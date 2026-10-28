"""Launch, stop and restart the serial bridge, and flip the board between RX and TX.

The bridge (tools/bridge.py in the sensors repo) reads the board's USB serial port
and POSTs readings back to this API. The RX/TX role is compiled into the board's
firmware, so switching means stopping the bridge (it holds the serial port),
running the repo's flash.sh, then starting the bridge again.
"""

import collections
import glob
import json
import math
import os
import re
import signal
import statistics
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import state_store
from .config import settings

MODES = ("rx", "tx")
FLASH_TIMEOUT = 180
# Connecting flushes whatever the board buffered and skips what it overflowed, so the first
# few packets after a (re)connect can jump in seq without that being a lossy link.
FRESH_PACKETS = 3
# Radio facts for the link maths. Sensitivity and SNR floor are the SX126x/SX127x figures for
# SF7 at 125 kHz; TX power, frequency and the send period come from the buoy firmware (lora.h).
SENSITIVITY_DBM = -123
SNR_FLOOR_DB = -7.5
TX_DBM = 14
FREQ_MHZ = 915
TX_PERIOD_S = 5
PACKET_KEEP_S = 24 * 3600
PACKET_FILE_MAX = 3_000_000
WATCH_SECONDS = 10  # how often the watchdog and the alert evaluator run
HEALTH_WINDOW = 20  # packets the loss rate is measured over
RATE_WINDOW_S = 60
LOG_LINES = 1000
LOG_ROTATE_BYTES = 5_000_000
STATUS_LOG_LINES = 500
# ponytail: macOS/Linux device names only; add COM ports if a Windows host runs telem.
PORT_GLOBS = ("/dev/tty.usbmodem*", "/dev/ttyACM*")

_RX = re.compile(r"LoRa RX #(\d+)\s+RSSI (-?\d+) dBm\s+SNR (-?\d+)")
_TX = re.compile(r"LoRa TX #(\d+)")
_AWAKE = re.compile(r"awake (\d+) ms \(duty ([\d.]+) %\)")
_CONNECTED = re.compile(r"connected to (\S+) @")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_STAMP = re.compile(r"^(\d\d:\d\d:\d\d)  (.*)$")
# Already "source  action  detail"; a raw "board | x" has a pipe after the source, so it does not match.
_CANON = re.compile(r"^(telem|bridge|board|flash)\s+(?!\|)\S")
_SENSOR_LINE = re.compile(r"(air|core|water|board|TDS|pH|turbidity)\s")

_lock = threading.RLock()
_proc = None
_log = collections.deque(maxlen=LOG_LINES)
_log_loaded = False
_base_url = "http://127.0.0.1:8000"
_mode = None  # the role last flashed from here; the board itself cannot be asked
_flashing = None
_dry_run = False
_port = None  # None = let the bridge find the ST-LINK itself
_keep = False  # the operator wants the bridge running: autostart it and restart it if it dies
_alerts = {"link": True, "hull": True, "stuck": False}  # which analytics may raise telem alerts
_prefs_loaded = False
_started_at = 0.0
_backoff = 0.0
_next_restart = 0.0
_stop_watch = threading.Event()
_watchdog = None
_connected = None
_link = {}
_added = 0  # lines ever logged: lets the dashboard ask only for what is new
_epoch = time.time_ns()  # changes when the log is cleared, so the dashboard starts over
_packets = collections.deque(maxlen=PACKET_KEEP_S // TX_PERIOD_S + 1000)
_packets_loaded = False
_pipe = {}
_outcomes = collections.deque(maxlen=HEALTH_WINDOW)  # True = packet arrived, False = one was lost
_arrivals = collections.deque()  # time of each recent packet


def service_name():
    return settings.receiver_service


def _load_prefs():
    global _prefs_loaded, _mode, _keep, _dry_run, _port, _base_url
    if _prefs_loaded:
        return
    _prefs_loaded = True
    p = state_store.load_receiver() or {}
    _mode = p.get("mode", _mode)
    _keep = bool(p.get("keep", False))
    _dry_run = bool(p.get("dry_run", _dry_run))
    _port = p.get("port", _port)
    _base_url = p.get("base_url", _base_url)
    _alerts.update({k: bool(v) for k, v in (p.get("alerts") or {}).items() if k in _alerts})


def _save_prefs():
    state_store.save_receiver({
        "mode": _mode, "keep": _keep, "dry_run": _dry_run, "port": _port,
        "base_url": _base_url, "alerts": _alerts,
    })


def set_alerts(**flags):
    """Choose which analytics may raise telem alerts: link, hull, stuck."""
    unknown = set(flags) - set(_alerts)
    if unknown:
        raise ValueError(f"unknown alert group: {', '.join(sorted(unknown))}")
    with _lock:
        _load_prefs()
        _alerts.update({k: bool(v) for k, v in flags.items() if v is not None})
        _save_prefs()
        return dict(_alerts)


def _dir():
    d = Path(settings.receiver_dir).expanduser()
    if not (d / "tools" / "bridge.py").is_file():
        raise ValueError(f"Sensors repo not found at {d}. Set RECEIVER_DIR.")
    return d


def _python(d):
    venv = d / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else "python3"


def ports():
    return sorted(p for g in PORT_GLOBS for p in glob.glob(g))


def _logfile():
    return Path(settings.state_db).parent / "receiver.log"


def _load_log():
    global _log_loaded
    if _log_loaded:
        return
    _log_loaded = True
    try:
        with open(_logfile(), encoding="utf-8", errors="replace") as f:
            _log.extend(_canon(l.rstrip("\n")) for l in collections.deque(f, maxlen=LOG_LINES))
            global _added
            _added += len(_log)
    except OSError:
        pass


def _packetfile():
    return Path(settings.state_db).parent / "packets.ndjson"


def _load_packets():
    global _packets_loaded
    if _packets_loaded:
        return
    _packets_loaded = True
    cutoff = time.time() - PACKET_KEEP_S
    try:
        with open(_packetfile(), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a half-written last line
                if rec["t"] >= cutoff:
                    _packets.append(rec)
    except OSError:
        pass


def _packets_after_load():
    _load_packets()
    return list(_packets)


def _record_packet(rec):
    _load_packets()
    _packets.append(rec)
    try:
        path = _packetfile()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _trim_packets():
    """Rewrite the file with only the last 24 h once it grows large; run at bridge start."""
    try:
        if _packetfile().stat().st_size <= PACKET_FILE_MAX:
            return
    except OSError:
        return
    _load_packets()
    cutoff = time.time() - PACKET_KEEP_S
    keep = [r for r in _packets if r["t"] >= cutoff]
    _packets.clear()
    _packets.extend(keep)
    _packetfile().write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")


def _histogram(values, width):
    """Counts per `width`-wide bin, contiguous from the lowest to the highest populated bin."""
    if not values:
        return {"lo": 0, "width": width, "counts": []}
    lo = math.floor(min(values) / width) * width
    counts = [0] * (int(math.floor(max(values) / width) * width - lo) // width + 1)
    for v in values:
        counts[int((math.floor(v / width) * width - lo) // width)] += 1
    return {"lo": lo, "width": width, "counts": counts}


def _summary(values):
    return {"min": min(values), "avg": round(statistics.fmean(values), 1), "max": max(values)} if values else None


def _window(recs, now, seconds):
    sel = [r for r in recs if r["t"] >= now - seconds]
    received, lost = len(sel), sum(r["lost"] for r in sel)
    # Intervals only between neighbours that arrived normally: not across connect bursts,
    # buoy restarts or a stretch with the bridge off.
    gaps = [
        b["t"] - a["t"]
        for a, b in zip(sel, sel[1:])
        if not (a["fresh"] or b["fresh"] or b["restart"]) and 0 < b["t"] - a["t"] <= 60
    ]
    return {
        "received": received,
        "lost": lost,
        "pdr": round(100 * received / (received + lost), 2) if received else None,
        "rssi": _summary([r["rssi"] for r in sel]),
        "snr": _summary([r["snr"] for r in sel]),
        "interval_s": {"mean": round(statistics.fmean(gaps), 2), "jitter": round(statistics.pstdev(gaps), 2),
                       "max": round(max(gaps), 1)} if gaps else None,
        "restarts": sum(1 for r in sel if r["restart"]),
        "events": [
            {"t": r["t"], "lost": r["lost"], "from": r["seq"] - r["lost"], "to": r["seq"] - 1, "restart": r["restart"]}
            for r in sel if r["lost"] or r["restart"]
        ][-15:],
    }


def _timeline(recs, now, buckets=48, width=1800):
    """Received and lost packets per half hour over the last 24 h, oldest first."""
    start = now - buckets * width
    out = [{"t": start + i * width, "received": 0, "lost": 0} for i in range(buckets)]
    for r in recs:
        i = int((r["t"] - start) // width)
        if 0 <= i < buckets:
            out[i]["received"] += 1
            out[i]["lost"] += r["lost"]
    return out


def packets(seq=None, limit=100):
    """Recent stored packets, newest first, optionally only those with one sequence number."""
    _load_packets()
    recs = [r for r in reversed(_packets) if seq is None or r["seq"] == seq]
    return recs[:limit]


def export_packets_csv():
    _load_packets()
    rows = ["time,seq,rssi_dbm,snr_db,lost_before,after_connect,buoy_restart"]
    for r in _packets:
        t = datetime.fromtimestamp(r["t"]).astimezone().isoformat(timespec="seconds")
        rows.append(f"{t},{r['seq']},{r['rssi']},{r['snr']},{r['lost']},{int(r['fresh'])},{int(r['restart'])}")
    return "\n".join(rows) + "\n"


def export_log_text():
    _load_log()
    return "\n".join(_log) + "\n"


def _spool_backlog():
    try:
        with open(Path(settings.receiver_dir).expanduser() / "tools" / "spool.ndjson", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def stats(now=None):
    """Link and pipeline analytics from the stored packets and this session's post counters."""
    now = time.time() if now is None else now
    _load_packets()
    recs = list(_packets)
    last = recs[-1] if recs else None
    day = [r for r in recs if r["t"] >= now - 86400]
    current = None
    if last:
        path_loss = TX_DBM - last["rssi"]  # antenna gains ignored
        metres = 1000 * 10 ** ((path_loss - 20 * math.log10(FREQ_MHZ) - 32.44) / 20)
        current = {
            "rssi": last["rssi"], "snr": last["snr"],
            "rssi_margin_db": last["rssi"] - SENSITIVITY_DBM,
            "snr_margin_db": last["snr"] - SNR_FLOOR_DB,
            "range_m_estimate": round(metres, 1),
            "buoy_uptime_s_estimate": last["seq"] * TX_PERIOD_S,
            "age_s": round(now - last["t"], 1),
        }
    ms = sorted(_pipe["ms"])
    return {
        "windows": {"1h": _window(recs, now, 3600), "24h": _window(recs, now, 86400)},
        "current": current,
        "timeline": _timeline(recs, now),
        "rssi_hist": _histogram([r["rssi"] for r in day], 10),
        "snr_hist": _histogram([r["snr"] for r in day], 3),
        "pipeline": {
            "posted": _pipe["ok"], "failed": _pipe["fail"], "dry": _pipe["dry"], "skipped": _pipe["skip"],
            "success_pct": round(100 * _pipe["ok"] / (_pipe["ok"] + _pipe["fail"]), 1) if _pipe["ok"] + _pipe["fail"] else None,
            # Packets that left no trace in the bridge output: neither posted, failed, skipped nor dry.
            "unaccounted": max(0, _link["received"] - _pipe["ok"] - _pipe["fail"] - _pipe["dry"] - _pipe["skip"]),
            "post_ms": {"avg": round(statistics.fmean(ms)), "p95": ms[min(len(ms) - 1, int(len(ms) * 0.95))],
                        "n": len(ms)} if ms else None,
            "spool_backlog": _spool_backlog(),
            "received": _link["received"],
        },
        "constants": {"sensitivity_dbm": SENSITIVITY_DBM, "snr_floor_db": SNR_FLOOR_DB, "tx_dbm": TX_DBM},
    }


def _push_service(**fields):
    """Report the radio link to Service health as a service of its own. Telem's service model
    is request-shaped, so: latency_ms = seconds between buoy packets (x1000), rps = packets per
    second, error_rate = % of the last packets that never arrived."""
    from . import generator

    name = service_name()
    if not generator.get_service_state(name):
        try:  # the buoy sends every ~5 s: one missed packet is a warning, three a critical
            generator.register_service(name, warn_ms=7500, crit_ms=15000)
        except ValueError:
            pass
    generator.ingest_service(name, **fields)


def _report_health(now, gap_ms, lost):
    """Called once per received packet; must never break the log pump."""
    _outcomes.extend([False] * lost)
    _outcomes.append(True)
    if gap_ms is not None:  # the connect flush arrives in a burst and would inflate the rate
        _arrivals.append(now)
    while _arrivals and now - _arrivals[0] > RATE_WINDOW_S:
        _arrivals.popleft()
    fields = {"error_rate": 100 * _outcomes.count(False) / len(_outcomes)}
    if gap_ms is not None:
        fields["latency_ms"] = gap_ms
    if len(_arrivals) >= 2 and now > _arrivals[0]:
        fields["rps"] = (len(_arrivals) - 1) / (now - _arrivals[0])
    try:
        _push_service(**fields)
    except Exception as exc:  # storage down, service deleted...
        print(f"[receiver] could not report link health: {exc}")


def _classify(body):
    """Map one raw bridge/board/flash line to (source, action, detail)."""
    if body.startswith("[telem] "):  # lines written before the format was normalised
        d = body[len("[telem] "):]
        act = "start" if d.startswith("starting") else "stop" if d.startswith("bridge stopped") else (
            "flash" if "flash" in d else "info")
        return "telem", act, d
    if m := re.match(r"board \| (.*)", body):
        d = m.group(1)
        if m := re.match(r"LoRa RX #(\d+)\s+(.*)", d):
            return "board", "rx", f"#{m[1]}  {m[2]}"
        if m := re.match(r"LoRa TX #(\d+):?\s*(.*)", d):
            return "board", "tx", f"#{m[1]}  {m[2]}".rstrip()
        if d.startswith("... listening"):
            return "board", "idle", d[4:]
        if re.match(r"waiting for|no device", d):
            return "board", "warn", d
        if d.startswith("----") or _SENSOR_LINE.match(d):
            return "board", "sensor", d
        if d.startswith("awake "):
            return "board", "power", d
        if '":' in d and not d.startswith("{"):  # tail of a JSON line cut by connecting mid-line
            return "board", "partial", d
        return "board", "log", d
    for prefix, action in (("sent", "post"), ("DRY", "dry"), ("FAIL", "fail"), ("skip", "skip")):
        if m := re.match(prefix + r"\s*\| (.*)", body):
            return "bridge", action, m.group(1)
    if m := re.match(r"flash \| (.*)", body):
        return "flash", "out", m.group(1)
    if body.startswith("connected to "):
        return "bridge", "connect", body[len("connected to "):]
    if body.startswith(("serial ", "no ST-LINK")):
        return "bridge", "serial", body
    if body.startswith("spool:"):
        return "bridge", "spool", body[len("spool:"):].strip()
    return "bridge", "log", body


def _canon(line):
    """One shape for every log line: `HH:MM:SS  source  action  detail`. Idempotent."""
    m = _STAMP.match(line)
    stamp, body = m.groups() if m else (f"{datetime.now():%H:%M:%S}", line)
    if _CANON.match(body):
        return f"{stamp}  {body}"
    src, act, detail = _classify(body)
    return f"{stamp}  {src:<6}  {act:<7}  {detail}"


_POST_OK = re.compile(r"\bsent \|.*\(HTTP 2\d\d(?:, (\d+) ms)?\)")


def _add(line):
    global _connected, _mode, _added
    _load_log()  # first, or the saved lines would load on top of ones added since
    raw, line = line, _canon(line)
    _log.append(line)
    _added += 1
    try:
        path = _logfile()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if m := _POST_OK.search(raw):
        _pipe["ok"] += 1
        if m.group(1):
            _pipe["ms"].append(int(m.group(1)))
    elif re.search(r"\bFAIL\s*\|", raw):
        _pipe["fail"] += 1
    elif re.search(r"\bDRY\s*\|", raw):
        _pipe["dry"] += 1
    elif re.search(r"\bskip\s*\|", raw):
        _pipe["skip"] += 1
    if m := _AWAKE.search(raw):
        _link["awake_ms"], _link["duty_pct"] = int(m.group(1)), float(m.group(2))
    elif "LoRa send FAILED" in raw:
        _link["tx_failed"] += 1
    if m := _CONNECTED.search(raw):
        _connected = m.group(1)
        _link["fresh"] = FRESH_PACKETS
    elif "serial lost" in raw or "no ST-LINK" in raw:
        _connected = None
    elif m := _TX.search(raw):
        _mode = "tx"  # the board's own output tells us its role better than our last flash does
        _link["tx_seq"] = int(m.group(1))
        _link["tx_sent"] += 1
    elif m := _RX.search(raw):
        _mode = "rx"
        seq = int(m.group(1))
        last, prev_at, now = _link.get("seq"), _link["at"], time.time()
        fresh = bool(_link["fresh"])
        lost = seq - last - 1 if last is not None and seq > last + 1 and not fresh else 0
        gap_ms = None if prev_at is None or fresh else round((now - prev_at) * 1000)
        _record_packet({"t": round(now, 1), "seq": seq, "rssi": int(m.group(2)), "snr": int(m.group(3)),
                        "lost": lost, "fresh": fresh, "restart": last is not None and seq <= last})
        _link["lost"] += lost
        _link["fresh"] = max(0, _link["fresh"] - 1)
        _link.update(
            seq=seq, rssi=int(m.group(2)), snr=int(m.group(3)), at=now,
            received=_link["received"] + 1,
        )
        _report_health(now, gap_ms, lost)


def _note(action, msg):
    _add(f"{datetime.now():%H:%M:%S}  {'telem':<6}  {action:<7}  {msg}")


def _reset_link():
    global _connected
    _connected = None
    _outcomes.clear()
    _arrivals.clear()
    _pipe.clear()
    _pipe.update(ok=0, fail=0, dry=0, skip=0, ms=collections.deque(maxlen=200))
    _link.clear()
    _link.update(received=0, lost=0, seq=None, rssi=None, snr=None, at=None, tx_seq=None,
                 tx_sent=0, tx_failed=0, awake_ms=None, duty_pct=None, fresh=FRESH_PACKETS)


_reset_link()


def _pump(proc):
    for line in proc.stdout:
        _add(line.rstrip())


def running():
    return _proc is not None and _proc.poll() is None


def status(since=None):
    """`since` is a log_total the caller already holds: only newer lines come back."""
    _load_log()
    _load_prefs()
    at = _link["at"]
    return {
        "running": running(),
        "keep": _keep,
        "alerts": dict(_alerts),
        "buoy": settings.receiver_buoy,
        "service": service_name(),
        "token_required": bool(settings.receiver_token),
        "mode": _mode,
        "flashing": _flashing,
        "dry_run": _dry_run,
        "port": _port,
        "connected_port": _connected if running() else None,
        "ports": ports(),
        "link": {**{k: v for k, v in _link.items() if k not in ("at", "fresh")},
                 "age_s": None if at is None else round(time.time() - at, 1)},
        "log": _log_since(since),
        "log_total": _added,
        "log_epoch": _epoch,
    }


def _log_since(since):
    lines = list(_log)
    if since is None:
        return lines[-STATUS_LOG_LINES:]
    return lines[max(0, since - (_added - len(lines))):][-STATUS_LOG_LINES:]


def clear_log():
    """Empty the console and the log file, or the old lines would load back on the next run."""
    global _epoch
    _load_log()  # so a later status() does not reload the file we are about to empty
    _log.clear()
    _epoch = time.time_ns()
    try:
        _logfile().write_text("", encoding="utf-8")
        _logfile().with_suffix(".log.1").unlink(missing_ok=True)
    except OSError:
        pass
    return status()


def _pidfile():
    return Path(settings.state_db).parent / "bridge.pid"


def reap_orphan():
    """A bridge left running by a telem that was killed hard still holds the serial port."""
    try:
        pid = int(_pidfile().read_text())
    except (OSError, ValueError):
        return
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return
    if "bridge.py" in cmd:  # not some unrelated process that inherited the pid
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
        _note("stop", f"stopped a bridge left running by an earlier telem (pid {pid})")
    _pidfile().unlink(missing_ok=True)


def _spawn():
    global _proc, _started_at
    if running():
        return
    d = _dir()
    reap_orphan()
    _load_log()
    try:
        if _logfile().stat().st_size > LOG_ROTATE_BYTES:
            _logfile().replace(_logfile().with_suffix(".log.1"))
    except OSError:
        pass
    _reset_link()
    _trim_packets()
    cmd = [_python(d), "-u", str(d / "tools" / "bridge.py"), "--base-url", _base_url,
           "--buoy-id", settings.receiver_buoy]
    if _dry_run:
        cmd.append("--dry-run")
    if _port:
        cmd += ["--port", _port]
    _note("start", f"bridge -> {_base_url}" + ("  [DRY RUN]" if _dry_run else ""))
    _proc = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _started_at = time.time()
    try:
        _pidfile().parent.mkdir(parents=True, exist_ok=True)
        _pidfile().write_text(str(_proc.pid))
    except OSError:
        pass
    threading.Thread(target=_pump, args=(_proc,), daemon=True).start()


def _terminate():
    global _proc
    if running():
        _proc.terminate()
        try:
            _proc.wait(10)
        except subprocess.TimeoutExpired:
            _proc.kill()
            _proc.wait()
        _note("stop", "bridge")
    _proc = None
    _pidfile().unlink(missing_ok=True)
    _reset_link()


def start(base_url=None, dry_run=None, port=None):
    """dry_run/port of None keep the previous choice; port "" means auto-detect.
    Starting also means "keep it running": telem restarts the bridge if it dies."""
    global _base_url, _dry_run, _port, _keep
    with _lock:
        _load_prefs()
        if running():
            return status()
        if port and port not in ports():
            raise ValueError(f"Serial port {port} is not present")
        if base_url:
            _base_url = base_url.rstrip("/")
        if dry_run is not None:
            _dry_run = dry_run
        if port is not None:
            _port = port or None
        _spawn()
        _keep = True
        _save_prefs()
        return status()


def stop():
    """The operator stopped it: stay stopped, including across telem restarts."""
    global _keep
    with _lock:
        _load_prefs()
        _keep = False
        _save_prefs()
        _terminate()
        return status()


def restart(base_url=None, dry_run=None, port=None):
    with _lock:
        _terminate()
        return start(base_url, dry_run, port)


def shutdown():
    """Telem is exiting: stop the bridge but leave the "keep running" choice for the next launch."""
    _stop_watch.set()
    with _lock:
        _terminate()


def _watch_bridge(now):
    """Restart a bridge that exited by itself, backing off while it keeps dying."""
    global _backoff, _next_restart
    with _lock:
        if not _keep or _flashing:
            return
        if running():
            if now - _started_at > 60:
                _backoff = 0
            return
        if now < _next_restart:
            return
        _backoff = min(60, max(3, _backoff * 2))
        _next_restart = now + _backoff
        _note("restart", "bridge exited unexpectedly, restarting")
        try:
            _spawn()
        except ValueError as exc:
            _note("fail", str(exc))


def tick(now=None):
    from . import receiver_alerts

    now = time.time() if now is None else now
    _watch_bridge(now)
    receiver_alerts.evaluate(now)


def _watch():
    while not _stop_watch.wait(WATCH_SECONDS):
        try:
            tick()
        except Exception as exc:  # the watcher must outlive any one bad tick
            print(f"[receiver] watchdog: {exc}")


def start_background():
    """At telem startup: clear any orphaned bridge, relaunch it if it was left on, then watch it."""
    global _watchdog
    with _lock:
        _load_prefs()
        reap_orphan()
        if _keep and not running():
            try:
                _spawn()
            except ValueError as exc:
                _note("fail", str(exc))
        if _watchdog is None or not _watchdog.is_alive():
            _stop_watch.clear()
            _watchdog = threading.Thread(target=_watch, name="receiver-watchdog", daemon=True)
            _watchdog.start()


def link_state(now):
    """What the alert evaluator needs to know about the radio link."""
    recent = [r for r in list(_packets)[-5:] if not r["fresh"]]
    return {
        "watching": running() and _mode != "tx" and not _flashing,
        "since_start_s": now - _started_at,
        "age_s": None if _link["at"] is None else now - _link["at"],
        "recent": recent,
        "outcomes": list(_outcomes),
    }


def _flash(d, mode):
    """Run flash.sh, streaming its output into the log. Returns (ok, last lines)."""
    p = subprocess.Popen(
        [str(d / "flash.sh"), mode], cwd=d,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    timer = threading.Timer(FLASH_TIMEOUT, p.kill)
    timer.start()
    tail = collections.deque(maxlen=12)
    for raw in p.stdout:
        line = _ANSI.sub("", raw).rstrip()
        if line:
            _add(f"flash | {line}")
            tail.append(line)
    p.wait()
    timer.cancel()
    ok = p.returncode == 0
    if not ok and p.returncode < 0:
        tail.append(f"flash.sh killed after {FLASH_TIMEOUT}s")
    return ok, "\n".join(tail)


def set_mode(mode, base_url=None):
    """Flash the board as `mode`, then bring the bridge back if it was running.
    On a failed flash the bridge is restarted anyway and ValueError carries the output."""
    global _mode, _flashing
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    with _lock:
        d = _dir()
        was_running = running()
        _terminate()
        _flashing = mode
        _note("flash", f"flashing board as {mode.upper()}...")
        try:
            ok, tail = _flash(d, mode)
        finally:
            _flashing = None
        if ok:
            _mode = mode
            _save_prefs()
            _note("flash", f"board flashed as {mode.upper()}")
        else:
            _note("flash", "FAILED")
        if was_running:
            _spawn()
        if not ok:
            raise ValueError(f"Flashing as {mode.upper()} failed: {tail}")
        return status()
