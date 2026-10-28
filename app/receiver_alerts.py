"""Turn link and sensor analytics into telem alerts.

Each group is a list of (severity, reason) pairs handed to generator.set_conditions(), which
raises, updates and clears alerts and incidents the same way a threshold breach does. An empty
list clears the group, so a condition that goes away resolves its incident by itself."""

import math
import statistics
import time

from . import generator, receiver
from .config import settings

SILENT_WARN_S, SILENT_CRIT_S = 20, 60
LOSS_WARN_PCT, LOSS_CRIT_PCT = 20, 50
HULL_RISE_WARN, HULL_RISE_CRIT = 5, 15  # %RH per hour; heuristics for a sealed hull
CONDENSATION_WARN_C = 3
STUCK_READINGS = 60  # five minutes of identical readings at one every 5 s
HISTORY_EVERY_S = 30
HISTORY_MINUTES = 60

_last_history = {"at": 0.0, "conditions": []}


def dew_point(temp_c, rh):
    if rh <= 0:
        return None
    g = math.log(rh / 100) + 17.62 * temp_c / (243.12 + temp_c)
    return 243.12 * g / (17.62 - g)


def link_conditions(state):
    if not state["watching"]:
        return []
    out = []
    silent = state["age_s"] if state["age_s"] is not None else (
        state["since_start_s"] if state["since_start_s"] > 30 else 0
    )
    if silent > SILENT_CRIT_S:
        out.append(("critical", f"no radio packets for {round(silent)} s"))
    elif silent > SILENT_WARN_S:
        out.append(("warning", f"no radio packets for {round(silent)} s"))
    recent = state["recent"]
    if len(recent) >= 3:
        snr = statistics.fmean(r["snr"] for r in recent) - receiver.SNR_FLOOR_DB
        rssi = statistics.fmean(r["rssi"] for r in recent) - receiver.SENSITIVITY_DBM
        if snr < 3 or rssi < 5:
            out.append(("critical", f"radio link margin nearly gone (SNR {snr:.1f} dB, RSSI {rssi:.0f} dB)"))
        elif snr < 6 or rssi < 10:
            out.append(("warning", f"radio link margin low (SNR {snr:.1f} dB, RSSI {rssi:.0f} dB)"))
    outcomes = state["outcomes"]
    if len(outcomes) >= 10:
        lost = 100 * outcomes.count(False) / len(outcomes)
        if lost >= LOSS_CRIT_PCT:
            out.append(("critical", f"{lost:.0f}% of recent packets lost"))
        elif lost >= LOSS_WARN_PCT:
            out.append(("warning", f"{lost:.0f}% of recent packets lost"))
    return out


def hull_conditions(points):
    """`points`: [{time (epoch s), core_humidity, core_temp, temp}] oldest first, from the last hour."""
    humid = [p for p in points if p.get("core_humidity") is not None]
    out = []
    if len(humid) >= 2 and humid[-1]["time"] - humid[0]["time"] >= 600:
        rate = (humid[-1]["core_humidity"] - humid[0]["core_humidity"]) / ((humid[-1]["time"] - humid[0]["time"]) / 3600)
        if rate > HULL_RISE_CRIT:
            out.append(("critical", f"hull humidity rising {rate:.0f} %RH/h (possible leak)"))
        elif rate > HULL_RISE_WARN:
            out.append(("warning", f"hull humidity rising {rate:.0f} %RH/h"))
    last = points[-1] if points else {}
    if None not in (last.get("core_humidity"), last.get("core_temp"), last.get("temp")):
        dp = dew_point(last["core_temp"], last["core_humidity"])
        if dp is not None:
            margin = last["temp"] - dp
            if margin < 0:
                out.append(("critical", f"condensation likely inside the hull (margin {margin:.1f} C)"))
            elif margin < CONDENSATION_WARN_C:
                out.append(("warning", f"condensation margin low inside the hull ({margin:.1f} C)"))
    return out


def stuck_conditions(series):
    """`series`: {sensor: [values]} oldest first."""
    out = []
    for name, values in sorted(series.items()):
        run = 0
        while run < len(values) and values[-1 - run] == values[-1]:
            run += 1
        if run >= STUCK_READINGS:
            out.append(("warning", f"{name}: reading flat for {run * receiver.TX_PERIOD_S // 60} min"))
    return out


def _history(now):
    """Buoy conditions from the last hour of readings; queried at most every 30 s."""
    if now - _last_history["at"] < HISTORY_EVERY_S:
        return _last_history["conditions"]
    _last_history["at"] = now
    from .influx_client import query_history

    buoy = settings.receiver_buoy
    state = generator.get_buoy_state(buoy)
    out = []
    if state and (receiver._alerts["hull"] or receiver._alerts["stuck"]):
        real = [k for k, s in state["sensors"].items() if not s["simulated"]]
        fields = sorted(set(real) | {"core_humidity", "core_temp", "temp"}) if receiver._alerts["stuck"] else \
            ["core_humidity", "core_temp", "temp"]
        rows = query_history("buoy_metrics", "buoy", buoy, fields, HISTORY_MINUTES, None, None, "raw", None)["points"]
        from datetime import datetime

        points = [
            {"time": datetime.fromisoformat(r["time"]).timestamp(), **r["values"]} for r in rows
        ]
        if points and now - points[-1]["time"] < 300:  # old data says nothing about now
            if receiver._alerts["hull"]:
                out += hull_conditions(points)
            if receiver._alerts["stuck"]:
                out += stuck_conditions({k: [p[k] for p in points if p.get(k) is not None] for k in real})
    _last_history["conditions"] = out
    return out


def evaluate(now=None):
    """Called by the watchdog. Never raises: a down database must not stop the bridge watcher."""
    now = time.time() if now is None else now
    service, buoy = receiver.service_name(), settings.receiver_buoy
    try:
        if generator.get_service_state(service):
            generator.set_conditions(
                "service", service,
                link_conditions(receiver.link_state(now)) if receiver._alerts["link"] else [],
            )
        if generator.get_buoy_state(buoy):
            generator.set_conditions("buoy", buoy, _history(now))
    except Exception as exc:
        print(f"[receiver] alert evaluation skipped: {exc}")
