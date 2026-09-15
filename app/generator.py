"""
Drives the whole fleet - services and buoys, whether they're part of the
built-in demo set or registered/ingested at runtime.

Each service's latency and each buoy's individual sensors/battery/position
carry their own "simulated" flag. While a field is simulated, the tick loop
does a bounded random walk and writes it to InfluxDB. The moment real data
is POSTed for that field via /api/ingest/..., that flag flips off and the
simulator leaves it alone from then on - real and fake data coexist on the
same fleet, down to individual sensors on the same buoy, without fighting
each other. POST .../simulate flips a field back to fake.

Alerts and incidents are derived from real threshold crossings on whichever
data is live for a field, real or simulated - the logic doesn't care which.
"""
import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import settings
from .influx_client import make_point, write_points, utcnow
from .seed_data import (
    SERVICES, BUOYS, METRIC_RANGES, DEFAULT_UNITS,
    GENERIC_SENSOR_BASE, GENERIC_SENSOR_VOLATILITY,
)

MOORING_LAT = sum(b["lat"] for b in BUOYS) / len(BUOYS)
MOORING_LNG = sum(b["lng"] for b in BUOYS) / len(BUOYS)

# ---------------------------------------------------------------- registries
_services: dict[str, dict] = {}
_buoys: dict[str, dict] = {}
_alerts: list[dict] = []
_incidents: dict[str, dict] = {}       # keyed by source name/id, one *open* incident per source
_incident_counter = 10                 # first generated incident reads as INC-011, alongside the demo fleet's INC-01x
_start_time = time.time()

KNOWN_BUOY_UNIVERSAL_FIELDS = {"battery", "lat", "lng", "satellites", "signal_dbm"}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _mean_revert_step(value, base, volatility, reversion=0.06, bias=0.0):
    delta = reversion * (base - value) + (random.random() - 0.5) * volatility
    if bias and random.random() < 0.15:
        delta += bias * random.uniform(0.5, 1.5)
    return value + delta


def _parse_time(t):
    if not t:
        return utcnow()
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return utcnow()


def _new_alert(severity: str, source_type: str, source: str, message: str) -> dict:
    alert = {
        "id": f"a{len(_alerts) + 1}",
        "time": utcnow().isoformat(),
        "severity": severity,
        "source_type": source_type,
        "source": source,
        "message": message,
    }
    _alerts.append(alert)
    del _alerts[:-200]  # keep the feed from growing unbounded on a long-running server
    return alert


def _service_status(latency, cfg):
    if latency >= cfg["crit_ms"]:
        return "critical"
    if latency >= cfg["warn_ms"]:
        return "warning"
    return "healthy"


def _buoy_status(state):
    """Status is deliberately conservative for anything outside the four
    well-known metrics: a custom sensor (registered or auto-added via
    ingest) is just plotted, never alerted on, since there's no notion of a
    "normal range" for it without validation."""
    if state["battery"] <= 15:
        return "critical", "low battery"
    sensors = state["sensors"]
    if "turbidity" in sensors and sensors["turbidity"]["value"] >= 8:
        return "warning", "sensor fault"
    if "ph" in sensors and not (6.0 <= sensors["ph"]["value"] <= 9.0):
        return "warning", "sensor fault"
    return "healthy", "healthy"


def _handle_transition(source_type, source, old_status, new_status, detail_message):
    """Edge-triggered alerting: only fires when status actually changes, and
    opens/closes an incident to match."""
    if old_status == new_status:
        return
    if new_status == "healthy":
        alert = _new_alert("resolved", source_type, source, f"{detail_message} cleared")
        incident = _incidents.get(source)
        if incident and incident["status"] == "ongoing":
            incident["status"] = "resolved"
            incident["resolved_at"] = alert["time"]
            incident["events"].append(alert)
    else:
        sev = "critical" if new_status == "critical" else "warning"
        alert = _new_alert(sev, source_type, source, detail_message)
        incident = _incidents.get(source)
        if not incident or incident["status"] == "resolved":
            global _incident_counter
            _incident_counter += 1
            incident = {
                "id": f"INC-{_incident_counter:03d}",
                "title": detail_message,
                "severity": sev,
                "status": "ongoing",
                "source_type": source_type,
                "source": source,
                "started_at": alert["time"],
                "resolved_at": None,
                "cause": _guess_cause(source_type, source),
                "events": [],
            }
            _incidents[source] = incident
        incident["severity"] = sev
        incident["title"] = detail_message
        incident["events"].append(alert)


def _guess_cause(source_type: str, source: str) -> str:
    if source_type == "service":
        return f"Correlates with recent load on {source} - no confirmed root cause yet."
    return f"Sensor or hardware condition on {source} - no confirmed root cause yet."


def _sensor_default_base(name):
    for b in BUOYS:
        if name in b.get("base", {}):
            return b["base"][name]
    return GENERIC_SENSOR_BASE


def _sensor_default_volatility(name):
    for b in BUOYS:
        if name in b.get("volatility", {}):
            return b["volatility"][name]
    return GENERIC_SENSOR_VOLATILITY


def _make_sensor_entry(name, unit=""):
    base = _sensor_default_base(name)
    return {"value": base, "unit": unit, "base": base, "volatility": _sensor_default_volatility(name), "simulated": True}


# ---------------------------------------------------------------- registration

def register_service(name, warn_ms=400.0, crit_ms=800.0, base_latency_ms=120.0, base_rps=500.0, volatility=20.0, bias=0.0):
    if name in _services:
        raise ValueError(f"service already exists: {name}")
    _services[name] = {
        "name": name,
        "warn_ms": warn_ms, "crit_ms": crit_ms,
        "base_latency_ms": base_latency_ms, "base_rps": base_rps, "volatility": volatility, "bias": bias,
        "latency": base_latency_ms, "rps": base_rps, "error_rate": 0.02,
        "status": "healthy",
        "simulated": True,
    }
    return _services[name]


def deregister_service(name):
    if name not in _services:
        raise KeyError(name)
    del _services[name]
    _incidents.pop(name, None)


def register_buoy(buoy_id, lat, lng, sensors=None, units=None):
    if buoy_id in _buoys:
        raise ValueError(f"buoy already exists: {buoy_id}")
    units = units or {}
    sensor_dict = {s: _make_sensor_entry(s, units.get(s, DEFAULT_UNITS.get(s, ""))) for s in (sensors or [])}
    _buoys[buoy_id] = {
        "id": buoy_id,
        "lat": lat, "lng": lng,
        "battery": 80.0, "battery_simulated": True, "solar_watts": 3.5, "battery_drain": 0.0,
        "satellites": random.randint(6, 9), "signal_dbm": -70.0, "position_simulated": True,
        "last_contact": time.time(),
        "status": "healthy", "status_text": "healthy",
        "sensors": sensor_dict,
    }
    return _buoys[buoy_id]


def deregister_buoy(buoy_id):
    if buoy_id not in _buoys:
        raise KeyError(buoy_id)
    del _buoys[buoy_id]
    _incidents.pop(buoy_id, None)


def add_buoy_sensor(buoy_id, sensor, unit=""):
    buoy = _buoys.get(buoy_id)
    if not buoy:
        raise KeyError(buoy_id)
    if sensor in buoy["sensors"]:
        raise ValueError(f"sensor already exists on {buoy_id}: {sensor}")
    buoy["sensors"][sensor] = _make_sensor_entry(sensor, unit or DEFAULT_UNITS.get(sensor, ""))
    return buoy["sensors"][sensor]


def remove_buoy_sensor(buoy_id, sensor):
    buoy = _buoys.get(buoy_id)
    if not buoy:
        raise KeyError(buoy_id)
    if sensor not in buoy["sensors"]:
        raise KeyError(sensor)
    del buoy["sensors"][sensor]


# ---------------------------------------------------------------- seeding

def seed_history():
    """Backfill ~history_minutes of 1-minute-resolution data for the built-in
    demo fleet, so its charts aren't empty on first load. Anything you
    register later starts fresh from "now", the way a real newly-deployed
    sensor would - no fabricated backstory."""
    minutes = settings.history_minutes
    now = utcnow()
    points = []

    for svc in SERVICES:
        register_service(
            svc["name"], svc["warn_ms"], svc["crit_ms"],
            svc["base_latency_ms"], svc["base_rps"], svc["volatility"], svc["bias"],
        )
        state = _services[svc["name"]]
        v, rps = svc["base_latency_ms"], svc["base_rps"]
        for i in range(minutes, 0, -1):
            v = _clamp(_mean_revert_step(v, svc["base_latency_ms"], svc["volatility"], bias=svc["bias"]), 10, 1500)
            rps = _clamp(rps + (random.random() - 0.5) * (svc["base_rps"] * 0.05), 1, svc["base_rps"] * 3)
            err = _clamp(random.random() * (2.0 if v > svc["crit_ms"] else 0.3), 0, 5)
            t = now - timedelta(minutes=i)
            points.append(make_point("service_metrics", {"service": svc["name"]}, {"latency_ms": v, "rps": rps, "error_rate": err}, time=t))
        state["latency"], state["rps"] = v, rps

    for b in BUOYS:
        register_buoy(b["id"], b["lat"], b["lng"], sensors=list(b["base"].keys()), units=DEFAULT_UNITS)
        state = _buoys[b["id"]]
        state["solar_watts"] = b["solar_watts"]
        state["battery_drain"] = b.get("battery_drain", 0.0)
        drift_cfg = {"turbidity": b.get("turbidity_drift", 0.0)}
        vals = dict(b["base"])
        battery = b["base_battery"]
        for i in range(minutes, 0, -1):
            for name, cfg in state["sensors"].items():
                lo, hi = METRIC_RANGES.get(name, (-1e9, 1e9))
                vals[name] = _clamp(_mean_revert_step(vals[name], cfg["base"], cfg["volatility"]) + drift_cfg.get(name, 0.0), lo, hi)
            if state["battery_drain"]:
                battery = _clamp(battery - state["battery_drain"] * random.uniform(0.5, 1.5), 3, 100)
            else:
                battery = _clamp(battery + (random.random() - 0.45) * 0.4, 20, 100)
            t = now - timedelta(minutes=i)
            points.append(make_point("buoy_metrics", {"buoy": b["id"]}, {**vals, "battery": battery}, time=t))
        for name in state["sensors"]:
            state["sensors"][name]["value"] = vals[name]
        state["battery"] = battery

    write_points(points)


# ---------------------------------------------------------------- live ticking

def _tick_once():
    now = utcnow()
    points = []

    for name, state in list(_services.items()):
        if not state["simulated"]:
            continue  # real data comes via ingest; the simulator leaves it alone
        old_status = state["status"]
        state["latency"] = _clamp(_mean_revert_step(state["latency"], state["base_latency_ms"], state["volatility"], bias=state["bias"]), 10, 1500)
        state["rps"] = _clamp(state["rps"] + (random.random() - 0.5) * (state["base_rps"] * 0.05), 1, state["base_rps"] * 4)
        state["error_rate"] = _clamp(random.random() * (2.0 if state["latency"] > state["crit_ms"] else 0.3), 0, 5)
        new_status = _service_status(state["latency"], state)
        state["status"] = new_status
        _handle_transition("service", name, old_status, new_status, f"{name} - p95 latency at {state['latency']:.0f}ms")
        points.append(make_point("service_metrics", {"service": name}, {"latency_ms": state["latency"], "rps": state["rps"], "error_rate": state["error_rate"]}, time=now))

    for buoy_id, state in list(_buoys.items()):
        old_status = state["status"]
        for sname, cfg in state["sensors"].items():
            if not cfg["simulated"]:
                continue
            lo, hi = METRIC_RANGES.get(sname, (-1e9, 1e9))
            cfg["value"] = _clamp(_mean_revert_step(cfg["value"], cfg["base"], cfg["volatility"]), lo, hi)
        if state["battery_simulated"]:
            if state.get("battery_drain"):
                state["battery"] = _clamp(state["battery"] - state["battery_drain"] * random.uniform(0.5, 1.5), 3, 100)
            else:
                state["battery"] = _clamp(state["battery"] + (random.random() - 0.45) * 0.4, 20, 100)
        if state["position_simulated"]:
            dist = ((state["lat"] - MOORING_LAT) ** 2 + (state["lng"] - MOORING_LNG) ** 2) ** 0.5
            state["signal_dbm"] = round(-58 - dist * 2200 + random.uniform(-4, 4), 1)
        state["last_contact"] = time.time() if random.random() > 0.05 else state["last_contact"]  # occasional missed check-in

        new_status, status_text = _buoy_status(state)
        state["status"] = new_status
        state["status_text"] = status_text
        if new_status != old_status:
            _handle_transition("buoy", buoy_id, old_status, new_status, f"{buoy_id} - {status_text}")

        fields = {sname: cfg["value"] for sname, cfg in state["sensors"].items() if cfg["simulated"]}
        if state["battery_simulated"]:
            fields["battery"] = state["battery"]
        if fields:
            points.append(make_point("buoy_metrics", {"buoy": buoy_id}, fields, time=now))

    if points:
        write_points(points)


async def background_loop():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _tick_once)
        except Exception as e:  # pragma: no cover - keep the simulator alive even if a write hiccups
            print(f"[generator] tick failed: {e}")
        await asyncio.sleep(settings.tick_seconds)


# ---------------------------------------------------------------- ingest (real data)

def ingest_service(name, latency_ms=None, rps=None, error_rate=None, time_str=None):
    state = _services.get(name)
    if not state:
        raise KeyError(name)
    fields = {}
    if latency_ms is not None:
        state["latency"] = latency_ms
        fields["latency_ms"] = latency_ms
    if rps is not None:
        state["rps"] = rps
        fields["rps"] = rps
    if error_rate is not None:
        state["error_rate"] = error_rate
        fields["error_rate"] = error_rate
    if not fields:
        return state
    state["simulated"] = False
    old_status = state["status"]
    new_status = _service_status(state["latency"], state)
    state["status"] = new_status
    _handle_transition("service", name, old_status, new_status, f"{name} - p95 latency at {state['latency']:.0f}ms")
    write_points([make_point("service_metrics", {"service": name}, fields, time=_parse_time(time_str))])
    return state


def ingest_service_batch(name, readings):
    for r in readings:
        ingest_service(name, r.get("latency_ms"), r.get("rps"), r.get("error_rate"), r.get("time"))


def ingest_buoy(buoy_id, fields_in: dict, time_str=None):
    """fields_in may contain the universal fields (battery/lat/lng/satellites/
    signal_dbm) plus any number of sensor readings by name, known or brand
    new - an unrecognized field name is auto-registered as a new sensor on
    this buoy rather than rejected, since there's no fixed schema per buoy."""
    state = _buoys.get(buoy_id)
    if not state:
        raise KeyError(buoy_id)
    old_status = state["status"]
    influx_fields = {}

    if fields_in.get("battery") is not None:
        state["battery"] = float(fields_in["battery"])
        state["battery_simulated"] = False
        influx_fields["battery"] = state["battery"]

    position_touched = False
    for key in ("lat", "lng"):
        if fields_in.get(key) is not None:
            state[key] = float(fields_in[key])
            position_touched = True
    if fields_in.get("satellites") is not None:
        state["satellites"] = int(fields_in["satellites"])
        position_touched = True
    if fields_in.get("signal_dbm") is not None:
        state["signal_dbm"] = float(fields_in["signal_dbm"])
        position_touched = True
    if position_touched:
        state["position_simulated"] = False

    for key, value in fields_in.items():
        if key in KNOWN_BUOY_UNIVERSAL_FIELDS or key == "time" or value is None:
            continue
        if key not in state["sensors"]:
            state["sensors"][key] = _make_sensor_entry(key, DEFAULT_UNITS.get(key, ""))
        state["sensors"][key]["value"] = float(value)
        state["sensors"][key]["simulated"] = False
        influx_fields[key] = float(value)

    if influx_fields:
        write_points([make_point("buoy_metrics", {"buoy": buoy_id}, influx_fields, time=_parse_time(time_str))])
    state["last_contact"] = time.time()

    new_status, status_text = _buoy_status(state)
    state["status"] = new_status
    state["status_text"] = status_text
    if new_status != old_status:
        _handle_transition("buoy", buoy_id, old_status, new_status, f"{buoy_id} - {status_text}")
    return state


def ingest_buoy_batch(buoy_id, readings):
    for r in readings:
        r = dict(r)
        t = r.pop("time", None)
        ingest_buoy(buoy_id, r, t)


def buoy_heartbeat(buoy_id, signal_dbm=None):
    state = _buoys.get(buoy_id)
    if not state:
        raise KeyError(buoy_id)
    state["last_contact"] = time.time()
    if signal_dbm is not None:
        state["signal_dbm"] = signal_dbm
        state["position_simulated"] = False
    return state


def resume_service_simulation(name):
    state = _services.get(name)
    if not state:
        raise KeyError(name)
    state["simulated"] = True
    return state


def resume_buoy_simulation(buoy_id, sensor=None):
    state = _buoys.get(buoy_id)
    if not state:
        raise KeyError(buoy_id)
    if sensor is None:
        state["battery_simulated"] = True
        state["position_simulated"] = True
        for cfg in state["sensors"].values():
            cfg["simulated"] = True
    elif sensor == "battery":
        state["battery_simulated"] = True
    elif sensor == "position":
        state["position_simulated"] = True
    elif sensor in state["sensors"]:
        state["sensors"][sensor]["simulated"] = True
    else:
        raise KeyError(sensor)
    return state


# ---------------------------------------------------------------- accessors used by routers

def get_service_state(name: str) -> Optional[dict]:
    return _services.get(name)


def get_all_service_states() -> dict[str, dict]:
    return _services


def get_buoy_state(buoy_id: str) -> Optional[dict]:
    return _buoys.get(buoy_id)


def get_all_buoy_states() -> dict[str, dict]:
    return _buoys


def get_alerts(limit: int = 20) -> list[dict]:
    return list(reversed(_alerts[-limit:]))


def get_incidents() -> list[dict]:
    return sorted(_incidents.values(), key=lambda i: i["started_at"], reverse=True)


def seconds_since_start() -> float:
    return time.time() - _start_time
