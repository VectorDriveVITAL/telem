"""Launch, stop and restart the serial bridge, and flip the board between RX and TX.

The bridge (tools/bridge.py in the sensors repo) reads the board's USB serial port
and POSTs readings back to this API. The RX/TX role is compiled into the board's
firmware, so switching means stopping the bridge (it holds the serial port),
running the repo's flash.sh, then starting the bridge again.
"""

import collections
import glob
import re
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
HEALTH_SERVICE = "lora-receiver"
HEALTH_WINDOW = 20  # packets the loss rate is measured over
RATE_WINDOW_S = 60
LOG_LINES = 1000
LOG_ROTATE_BYTES = 5_000_000
STATUS_LOG_LINES = 500
# ponytail: macOS/Linux device names only; add COM ports if a Windows host runs telem.
PORT_GLOBS = ("/dev/tty.usbmodem*", "/dev/ttyACM*")

_RX = re.compile(r"LoRa RX #(\d+)\s+RSSI (-?\d+) dBm\s+SNR (-?\d+)")
_TX = re.compile(r"LoRa TX #(\d+)")
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
_connected = None
_link = {}
_outcomes = collections.deque(maxlen=HEALTH_WINDOW)  # True = packet arrived, False = one was lost
_arrivals = collections.deque()  # time of each recent packet


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
    except OSError:
        pass


def _push_service(**fields):
    """Report the radio link to Service health as a service of its own. Telem's service model
    is request-shaped, so: latency_ms = seconds between buoy packets (x1000), rps = packets per
    second, error_rate = % of the last packets that never arrived."""
    from . import generator

    if not generator.get_service_state(HEALTH_SERVICE):
        try:  # the buoy sends every ~5 s: one missed packet is a warning, three a critical
            generator.register_service(HEALTH_SERVICE, warn_ms=7500, crit_ms=15000)
        except ValueError:
            pass
    generator.ingest_service(HEALTH_SERVICE, **fields)


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


def _add(line):
    global _connected, _mode
    raw, line = line, _canon(line)
    _log.append(line)
    try:
        path = _logfile()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if m := _CONNECTED.search(raw):
        _connected = m.group(1)
        _link["fresh"] = FRESH_PACKETS
    elif "serial lost" in raw or "no ST-LINK" in raw:
        _connected = None
    elif m := _TX.search(raw):
        _mode = "tx"  # the board's own output tells us its role better than our last flash does
        _link["tx_seq"] = int(m.group(1))
    elif m := _RX.search(raw):
        _mode = "rx"
        seq = int(m.group(1))
        last, prev_at, now = _link.get("seq"), _link["at"], time.time()
        lost = seq - last - 1 if last is not None and seq > last + 1 and not _link["fresh"] else 0
        gap_ms = None if prev_at is None or _link["fresh"] else round((now - prev_at) * 1000)
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
    _link.clear()
    _link.update(received=0, lost=0, seq=None, rssi=None, snr=None, at=None, tx_seq=None,
                 fresh=FRESH_PACKETS)


_reset_link()


def _pump(proc):
    for line in proc.stdout:
        _add(line.rstrip())


def running():
    return _proc is not None and _proc.poll() is None


def status():
    global _mode
    _load_log()
    if _mode is None:
        _mode = (state_store.load_receiver() or {}).get("mode")
    at = _link["at"]
    return {
        "running": running(),
        "mode": _mode,
        "flashing": _flashing,
        "dry_run": _dry_run,
        "port": _port,
        "connected_port": _connected if running() else None,
        "ports": ports(),
        "link": {**{k: v for k, v in _link.items() if k not in ("at", "fresh")},
                 "age_s": None if at is None else round(time.time() - at, 1)},
        "log": list(_log)[-STATUS_LOG_LINES:],
    }


def clear_log():
    _log.clear()
    return status()


def start(base_url=None, dry_run=None, port=None):
    """dry_run/port of None keep the previous choice; port "" means auto-detect."""
    global _proc, _base_url, _dry_run, _port
    with _lock:
        if running():
            return status()
        d = _dir()
        if port and port not in ports():
            raise ValueError(f"Serial port {port} is not present")
        _base_url = (base_url or _base_url).rstrip("/")
        if dry_run is not None:
            _dry_run = dry_run
        if port is not None:
            _port = port or None
        _load_log()
        try:
            if _logfile().stat().st_size > LOG_ROTATE_BYTES:
                _logfile().replace(_logfile().with_suffix(".log.1"))
        except OSError:
            pass
        _reset_link()
        cmd = [_python(d), "-u", str(d / "tools" / "bridge.py"), "--base-url", _base_url]
        if _dry_run:
            cmd.append("--dry-run")
        if _port:
            cmd += ["--port", _port]
        _note("start", f"bridge -> {_base_url}" + ("  [DRY RUN]" if _dry_run else ""))
        _proc = subprocess.Popen(
            cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        threading.Thread(target=_pump, args=(_proc,), daemon=True).start()
        return status()


def stop():
    global _proc
    with _lock:
        if running():
            _proc.terminate()
            try:
                _proc.wait(10)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait()
            _note("stop", "bridge")
        _proc = None
        _reset_link()
        return status()


def restart(base_url=None, dry_run=None, port=None):
    with _lock:
        stop()
        return start(base_url, dry_run, port)


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
        stop()
        _flashing = mode
        _note("flash", f"flashing board as {mode.upper()}...")
        try:
            ok, tail = _flash(d, mode)
        finally:
            _flashing = None
        if ok:
            _mode = mode
            state_store.save_receiver({"mode": mode})
            _note("flash", f"board flashed as {mode.upper()}")
        else:
            _note("flash", "FAILED")
        if was_running:
            start(base_url)
        if not ok:
            raise ValueError(f"Flashing as {mode.upper()} failed: {tail}")
        return status()
