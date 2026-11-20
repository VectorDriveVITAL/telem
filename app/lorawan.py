"""Link health per LoRaWAN device, fed by pi/lorawan_adapter.py (one POST per uplink).

Kept in memory: a telem restart starts the counts again. The readings themselves go through
/api/ingest as usual; this only tracks how well the radio link is doing."""

import statistics
import threading
from collections import deque
from datetime import datetime, timezone

_lock = threading.Lock()
_devices = {}
_events = deque(maxlen=200)
SILENT_FACTOR = 3  # a device is silent after this many typical gaps without an uplink
SILENT_MIN_S = 60


def record(dev_eui, buoy, f_cnt, rssi, snr, now=None):
    now = now or datetime.now(timezone.utc)
    with _lock:
        d = _devices.setdefault(
            dev_eui, {"uplinks": 0, "lost": 0, "restarts": 0, "f_cnt": None, "last": None, "gaps": deque(maxlen=8)}
        )
        note = ""
        if d["f_cnt"] is not None and f_cnt is not None:
            if f_cnt > d["f_cnt"]:
                lost = f_cnt - d["f_cnt"] - 1
                d["lost"] += lost
                note = f"{lost} lost" if lost else ""
            else:  # the counter went back: a rejoin or a reset, not a loss
                d["restarts"] += 1
                note = "restarted"
        if d["last"] is not None:
            d["gaps"].append((now - d["last"]).total_seconds())
        d.update(buoy=buoy, f_cnt=f_cnt, last=now, rssi=rssi, snr=snr)
        d["uplinks"] += 1
        _events.appendleft({"time": now.isoformat(), "dev_eui": dev_eui, "buoy": buoy, "f_cnt": f_cnt,
                            "rssi": rssi, "snr": snr, "note": note})


def status(now=None):
    now = now or datetime.now(timezone.utc)
    with _lock:
        devices = []
        for eui, d in _devices.items():
            age = (now - d["last"]).total_seconds()
            interval = statistics.median(d["gaps"]) if d["gaps"] else None
            sent = d["uplinks"] + d["lost"]
            devices.append({
                "dev_eui": eui, "buoy": d["buoy"], "f_cnt": d["f_cnt"], "rssi": d["rssi"], "snr": d["snr"],
                "uplinks": d["uplinks"], "lost": d["lost"], "restarts": d["restarts"],
                "delivery": round(100 * d["uplinks"] / sent, 1), "last": d["last"].isoformat(),
                "age_s": round(age, 1), "interval_s": interval,
                "silent": interval is not None and age > max(SILENT_FACTOR * interval, SILENT_MIN_S),
            })
        return {"devices": sorted(devices, key=lambda x: x["buoy"] or "~" + x["dev_eui"]), "events": list(_events)[:100]}


def reset():
    with _lock:
        _devices.clear()
        _events.clear()
