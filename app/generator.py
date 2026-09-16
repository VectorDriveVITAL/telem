"""Single-process fleet controller; durable operations plus real/demo telemetry.

Every mutation is serialized and checkpointed in SQLite. InfluxDB remains the
sample store. A failed sample write rolls back the in-memory mutation. These two
stores do not share a distributed transaction; a retry uses the same sample
identity (source, field, timestamp) and therefore overwrites that sample.
"""

import asyncio
import copy
import functools
import math
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import state_store
from .config import settings
from .influx_client import make_point, utcnow, write_points
from .models import SensorRules
from .seed_data import BUOYS, DEFAULT_UNITS, METRIC_RANGES, SERVICES

KNOWN_BUOY_UNIVERSAL_FIELDS = {
    "battery",
    "lat",
    "lng",
    "satellites",
    "signal_dbm",
    "solar_watts",
}
UNITS = {
    "battery": "%",
    "lat": "°",
    "lng": "°",
    "satellites": "",
    "signal_dbm": "dBm",
    "solar_watts": "W",
}
_lock = threading.RLock()
_depth = 0
_start_time = time.time()
_state = {
    "services": {},
    "buoys": {},
    "alerts": [],
    "incidents": {},
    "maintenance": [],
    "mutes": {},
}


def mutation(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global _state, _depth
        with _lock:
            outer = _depth == 0
            before = copy.deepcopy(_state) if outer else None
            _depth += 1
            try:
                result = fn(*args, **kwargs)
                if outer:
                    state_store.save(_state)
                return copy.deepcopy(result)
            except Exception:
                if outer:
                    _state = before
                raise
            finally:
                _depth -= 1

    return wrapped


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _parse_time(value):
    if not value:
        return utcnow()
    dt = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age(iso):
    return (
        max(0, (utcnow() - _parse_time(iso)).total_seconds()) if iso else float("inf")
    )


def _walk(value, base, volatility):
    return value + 0.06 * (base - value) + (random.random() - 0.5) * volatility


def _sensor(name, unit=""):
    base = next((b["base"][name] for b in BUOYS if name in b["base"]), 0.0)
    volatility = next(
        (b["volatility"][name] for b in BUOYS if name in b["volatility"]), 1.0
    )
    rules = SensorRules().model_dump()
    if name == "ph":
        rules.update(warn_min=6.0, warn_max=9.0)
    if name == "turbidity":
        rules["warn_max"] = 8.0
    return {
        "value": base,
        "base": base,
        "volatility": volatility,
        "unit": unit,
        "simulated": True,
        "last_reading_at": None,
        "rules": rules,
    }


def _source(kind, source):
    registry = _state["services" if kind == "service" else "buoys"]
    if source not in registry:
        raise KeyError(f"unknown {kind}: {source}")
    return registry[source]


def _alert(severity, kind, source, message):
    alert = {
        "id": uuid.uuid4().hex,
        "time": utcnow().isoformat(),
        "severity": severity,
        "source_type": kind,
        "source": source,
        "message": message,
        "acknowledged_at": None,
    }
    _state["alerts"].append(alert)
    # Incident timelines retain their own copies even as the recent feed expires.
    del _state["alerts"][:-2000]
    return alert


def _transition(kind, source, status, reasons):
    state = _source(kind, source)
    fingerprint = "|".join([status, *sorted(reasons)])
    old = state.get("condition", "healthy")
    state.update(
        status=status,
        status_text="; ".join(reasons) or "healthy",
        condition=fingerprint,
    )
    if old == fingerprint:
        return
    active = next(
        (
            i
            for i in _state["incidents"].values()
            if i["source_type"] == kind
            and i["source"] == source
            and i["status"] == "ongoing"
        ),
        None,
    )
    if status == "healthy":
        if old == "healthy":
            return
        event = _alert("resolved", kind, source, f"{source} - conditions cleared")
        if active:
            active.update(status="resolved", resolved_at=event["time"])
            active["events"].append(event)
        return
    event = _alert(status, kind, source, f"{source} - {'; '.join(reasons)}")
    if active is None:
        iid = "INC-" + uuid.uuid4().hex[:10].upper()
        active = {
            "id": iid,
            "title": event["message"],
            "severity": status,
            "status": "ongoing",
            "source_type": kind,
            "source": source,
            "started_at": event["time"],
            "resolved_at": None,
            "cause": "No confirmed root cause yet.",
            "events": [],
            "owner": "",
            "acknowledged_at": None,
            "notes": [],
        }
        _state["incidents"][iid] = active
    active.update(severity=status, title=event["message"])
    active["events"].append(event)


def _sensor_health(sensor):
    rules = sensor["rules"]
    stale = _age(sensor.get("last_reading_at")) > rules["stale_after_seconds"]
    if not rules["enabled"]:
        return "healthy", stale
    if stale:
        return "warning", True
    value = sensor["value"]
    for severity, prefix in [("critical", "crit"), ("warning", "warn")]:
        lo, hi = rules[prefix + "_min"], rules[prefix + "_max"]
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return severity, False
    return "healthy", False


def _buoy_health(state):
    reasons, status = [], "healthy"
    if time.time() - state["last_contact"] > state["offline_after_seconds"]:
        reasons.append("offline")
        status = "critical"
    if state["battery"] <= 15:
        reasons.append("low battery")
        status = "critical"
    for name, sensor in state["sensors"].items():
        severity, stale = _sensor_health(sensor)
        if severity != "healthy":
            reasons.append(
                f"{name}: "
                + ("stale reading" if stale else f"{severity} limit exceeded")
            )
            if status != "critical":
                status = severity
    return status, reasons


def _service_health(state):
    if _age(state.get("last_reading_at")) > 300:
        return "warning", ["stale reading"]
    severity = (
        "critical"
        if state["latency"] >= state["crit_ms"]
        else "warning"
        if state["latency"] >= state["warn_ms"]
        else "healthy"
    )
    return severity, ["latency threshold exceeded"] if severity != "healthy" else []


def _evaluate():
    for kind, registry in [("service", _state["services"]), ("buoy", _state["buoys"])]:
        for name, state in registry.items():
            status, reasons = (
                _service_health(state) if kind == "service" else _buoy_health(state)
            )
            _transition(kind, name, status, reasons)


@mutation
def register_service(
    name,
    warn_ms=400.0,
    crit_ms=800.0,
    base_latency_ms=120.0,
    base_rps=500.0,
    volatility=20.0,
    bias=0.0,
):
    if name in _state["services"]:
        raise ValueError(f"service already exists: {name}")
    state = {
        "name": name,
        "warn_ms": warn_ms,
        "crit_ms": crit_ms,
        "base_latency_ms": base_latency_ms,
        "base_rps": base_rps,
        "volatility": volatility,
        "bias": bias,
        "latency": base_latency_ms,
        "rps": base_rps,
        "error_rate": 0.02,
        "status": "healthy",
        "simulated": True,
        "last_reading_at": None,
        "field_times": {},
        "latency_delta_ms": 0,
    }
    _state["services"][name] = state
    return state


@mutation
def register_buoy(buoy_id, lat, lng, sensors=None, units=None):
    if buoy_id in _state["buoys"]:
        raise ValueError(f"buoy already exists: {buoy_id}")
    if any(s in KNOWN_BUOY_UNIVERSAL_FIELDS or s == "time" for s in sensors or []):
        raise ValueError(
            "sensor names cannot shadow battery, position, solar, signal or time fields"
        )
    units = units or {}
    state = {
        "id": buoy_id,
        "lat": lat,
        "lng": lng,
        "mooring_lat": lat,
        "mooring_lng": lng,
        "battery": 80.0,
        "battery_simulated": True,
        "battery_drain": 0.0,
        "solar_watts": 3.5,
        "satellites": 8,
        "signal_dbm": -70.0,
        "position_simulated": True,
        "contact_simulated": True,
        "last_contact": time.time(),
        "offline_after_seconds": 300,
        "status": "healthy",
        "status_text": "healthy",
        "field_metadata": {
            key: {"simulated": True, "last_reading_at": None, "unit": UNITS[key]}
            for key in KNOWN_BUOY_UNIVERSAL_FIELDS
        },
        "sensors": {
            s: _sensor(s, units.get(s, DEFAULT_UNITS.get(s, ""))) for s in sensors or []
        },
    }
    _state["buoys"][buoy_id] = state
    return state


@mutation
def deregister_service(name):
    _source("service", name)
    _close_for_removal("service", name)
    del _state["services"][name]


@mutation
def deregister_buoy(buoy_id):
    _source("buoy", buoy_id)
    _close_for_removal("buoy", buoy_id)
    del _state["buoys"][buoy_id]


def _close_for_removal(kind, source):
    for incident in _state["incidents"].values():
        if (
            incident["source_type"] == kind
            and incident["source"] == source
            and incident["status"] == "ongoing"
        ):
            event = _alert(
                "resolved", kind, source, "Source deregistered; history retained."
            )
            incident.update(status="resolved", resolved_at=event["time"])
            incident["events"].append(event)


@mutation
def add_buoy_sensor(buoy_id, sensor, unit=""):
    buoy = _source("buoy", buoy_id)
    if sensor in KNOWN_BUOY_UNIVERSAL_FIELDS or sensor == "time":
        raise ValueError("reserved telemetry field")
    if sensor in buoy["sensors"]:
        raise ValueError(f"sensor already exists: {sensor}")
    if len(buoy["sensors"]) >= 64:
        raise ValueError("maximum 64 sensors per buoy")
    buoy["sensors"][sensor] = _sensor(sensor, unit or DEFAULT_UNITS.get(sensor, ""))
    return buoy["sensors"][sensor]


@mutation
def remove_buoy_sensor(buoy_id, sensor):
    del _source("buoy", buoy_id)["sensors"][sensor]
    _evaluate()


def _seed_demo_fields():
    """Backfill simulated fields once per source/field/horizon, including upgrades.

    Keep restored latest readings and operator state untouched. Real fields are
    excluded, even on mixed real/demo buoys. Minute-aligned seed timestamps make
    retrying a failed startup overwrite the same samples rather than duplicate them.
    """
    if not settings.seed_demo or settings.history_minutes <= 0:
        return
    now = utcnow().replace(second=0, microsecond=0)
    seeded = _state.setdefault("demo_seeded_fields", {})
    points = []
    for kind, registry in [("service", "services"), ("buoy", "buoys")]:
        for name, state in _state[registry].items():
            if kind == "service":
                fields = (
                    {
                        "latency_ms": state["latency"],
                        "rps": state["rps"],
                        "error_rate": state["error_rate"],
                    }
                    if state["simulated"]
                    else {}
                )
            else:
                fields = {
                    key: sensor["value"]
                    for key, sensor in state["sensors"].items()
                    if sensor["simulated"]
                }
                fields.update(
                    {
                        key: state[key]
                        for key, meta in state["field_metadata"].items()
                        if meta["simulated"]
                    }
                )
            fields = {
                key: value
                for key, value in fields.items()
                if seeded.get(f"{kind}/{name}/{key}", 0) < settings.history_minutes
            }
            if not fields:
                continue
            rng = random.Random(f"demo-v2/{kind}/{name}")
            phases = {key: rng.random() * math.tau for key in sorted(fields)}
            for offset in range(settings.history_minutes, 0, -1):
                ts = now - timedelta(minutes=offset)
                hour = ts.hour + ts.minute / 60
                values = {}
                for key, base in fields.items():
                    wave = math.sin(offset / 37 + phases[key]) + 0.35 * math.sin(
                        offset / 7 + phases[key]
                    )
                    noise = rng.uniform(-0.08, 0.08)
                    if key in {"lat", "lng"}:
                        value = base + 0.000025 * wave
                        value = max(
                            -90 if key == "lat" else -180,
                            min(90 if key == "lat" else 180, value),
                        )
                    elif key == "battery":
                        value = max(0, min(100, base + 5 * wave))
                    elif key == "solar_watts":
                        value = (
                            max(0, max(1, base) * math.sin(math.pi * (hour - 6) / 12))
                            if 6 < hour < 18
                            else 0
                        )
                    elif key == "satellites":
                        value = max(0, round(base + 2 * wave))
                    elif key == "signal_dbm":
                        value = min(0, base + 5 * wave)
                    elif key == "error_rate":
                        value = max(0, min(100, base + 0.3 + 0.3 * wave))
                    else:
                        amplitude = max(abs(base) * 0.08, 0.2)
                        value = base + amplitude * (wave + noise)
                        if key in METRIC_RANGES:
                            lo, hi = METRIC_RANGES[key]
                            value = max(lo, min(hi, value))
                        elif key in {"latency_ms", "rps"}:
                            value = max(0, value)
                    values[key] = value
                points.append(
                    make_point(
                        "service_metrics" if kind == "service" else "buoy_metrics",
                        {
                            kind: name,
                            "data_source": "simulated",
                        },
                        values,
                        ts,
                    )
                )
                if len(points) >= 1000:
                    write_points(points)
                    points = []
            for key in fields:
                seeded[f"{kind}/{name}/{key}"] = settings.history_minutes
    if points:
        write_points(points)


@mutation
def seed_history():
    """Restore the fleet and upgrade missing demo history without resetting it."""
    global _state
    restored = state_store.load()
    if restored is not None:
        _state = restored
    elif settings.seed_demo:
        for svc in SERVICES:
            register_service(
                svc["name"],
                svc["warn_ms"],
                svc["crit_ms"],
                svc["base_latency_ms"],
                svc["base_rps"],
                svc["volatility"],
                svc["bias"],
            )
        for b in BUOYS:
            register_buoy(b["id"], b["lat"], b["lng"], list(b["base"]), DEFAULT_UNITS)
            _state["buoys"][b["id"]].update(
                battery=b["base_battery"],
                battery_drain=b.get("battery_drain", 0),
                solar_watts=b["solar_watts"],
            )
    _seed_demo_fields()
    _evaluate()


@mutation
def _tick_once():
    now, points = utcnow(), []
    for name, s in _state["services"].items():
        if not s["simulated"]:
            continue
        previous = s["latency"]
        s["latency"] = max(
            0.0,
            _walk(previous, s["base_latency_ms"], s["volatility"])
            + s["bias"] * random.random(),
        )
        s["latency_delta_ms"] = s["latency"] - previous
        s["rps"] = max(1.0, _walk(s["rps"], s["base_rps"], s["base_rps"] * 0.05))
        s["error_rate"] = random.random() * (2 if s["latency"] > s["crit_ms"] else 0.3)
        s["last_reading_at"] = now.isoformat()
        points.append(
            make_point(
                "service_metrics",
                {"service": name, "data_source": "simulated"},
                {
                    "latency_ms": s["latency"],
                    "rps": s["rps"],
                    "error_rate": s["error_rate"],
                },
                now,
            )
        )
    for name, b in _state["buoys"].items():
        fields = {}
        for metric, s in b["sensors"].items():
            if s["simulated"]:
                lo, hi = METRIC_RANGES.get(metric, (-1e9, 1e9))
                s["value"] = max(
                    lo, min(hi, _walk(s["value"], s["base"], s["volatility"]))
                )
                s["last_reading_at"] = now.isoformat()
                fields[metric] = s["value"]
        for key, meta in b["field_metadata"].items():
            if not meta["simulated"]:
                continue
            if key == "battery":
                b[key] = max(
                    0,
                    min(
                        100,
                        b[key] - b["battery_drain"] + (random.random() - 0.45) * 0.2,
                    ),
                )
            elif key == "signal_dbm":
                b[key] = -70 + random.uniform(-4, 4)
            meta["last_reading_at"] = now.isoformat()
            fields[key] = b[key]
        # A real device's heartbeats are NEVER fabricated by simulated fields.
        if b["contact_simulated"]:
            b["last_contact"] = time.time()
        if fields:
            points.append(
                make_point(
                    "buoy_metrics",
                    {"buoy": name, "data_source": "simulated"},
                    fields,
                    now,
                )
            )
    if points:
        write_points(points)
    _evaluate()


async def background_loop():
    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except Exception as exc:
            print(f"[fleet] tick failed: {type(exc).__name__}")
        await asyncio.sleep(settings.tick_seconds)


@mutation
def ingest_service(name, latency_ms=None, rps=None, error_rate=None, time_str=None):
    s = _source("service", name)
    fields = {
        k: v
        for k, v in {
            "latency_ms": latency_ms,
            "rps": rps,
            "error_rate": error_rate,
        }.items()
        if v is not None
    }
    if not fields:
        return s
    ts = _parse_time(time_str)
    write_points(
        [
            make_point(
                "service_metrics", {"service": name, "data_source": "real"}, fields, ts
            )
        ]
    )
    for key, value in fields.items():
        if (
            s["simulated"]
            or key not in s["field_times"]
            or ts >= _parse_time(s["field_times"][key])
        ):
            if key == "latency_ms":
                s["latency_delta_ms"] = value - s["latency"]
            s["latency" if key == "latency_ms" else key] = value
            s["field_times"][key] = ts.isoformat()
    # Arrival of older backfill does not move the last reading backwards.
    s["last_reading_at"] = max(s["field_times"].values(), key=_parse_time)
    s["simulated"] = False
    _evaluate()
    return s


@mutation
def ingest_service_batch(name, readings):
    for r in readings:
        ingest_service(
            name, r.get("latency_ms"), r.get("rps"), r.get("error_rate"), r.get("time")
        )


@mutation
def ingest_buoy(buoy_id, fields_in, time_str=None):
    b = _source("buoy", buoy_id)
    fields = {
        k: float(v) for k, v in fields_in.items() if v is not None and k != "time"
    }
    unknown = set(fields) - KNOWN_BUOY_UNIVERSAL_FIELDS - set(b["sensors"])
    if len(b["sensors"]) + len(unknown) > 64:
        raise ValueError("maximum 64 sensors per buoy")
    ts = _parse_time(time_str)
    if fields:
        write_points(
            [
                make_point(
                    "buoy_metrics", {"buoy": buoy_id, "data_source": "real"}, fields, ts
                )
            ]
        )
    for key, value in fields.items():
        if key in KNOWN_BUOY_UNIVERSAL_FIELDS:
            meta = b["field_metadata"][key]
            target = b
        else:
            if key not in b["sensors"]:
                b["sensors"][key] = _sensor(key, DEFAULT_UNITS.get(key, ""))
            meta = target = b["sensors"][key]
        if (
            meta["simulated"]
            or not meta["last_reading_at"]
            or ts >= _parse_time(meta["last_reading_at"])
        ):
            target[key if target is b else "value"] = (
                int(value) if key == "satellites" else value
            )
            meta["last_reading_at"] = ts.isoformat()
        meta["simulated"] = False
    b["battery_simulated"] = b["field_metadata"]["battery"]["simulated"]
    b["position_simulated"] = all(
        b["field_metadata"][k]["simulated"]
        for k in ("lat", "lng", "satellites", "signal_dbm")
    )
    b.update(last_contact=time.time(), contact_simulated=False)
    _evaluate()
    return b


@mutation
def ingest_buoy_batch(buoy_id, readings):
    for reading in readings:
        r = dict(reading)
        ts = r.pop("time", None)
        ingest_buoy(buoy_id, r, ts)


@mutation
def buoy_heartbeat(buoy_id, signal_dbm=None):
    return ingest_buoy(
        buoy_id, {"signal_dbm": signal_dbm} if signal_dbm is not None else {}
    )


@mutation
def resume_service_simulation(name):
    s = _source("service", name)
    s.update(simulated=True, field_times={})
    return s


@mutation
def resume_buoy_simulation(buoy_id, sensor=None):
    b = _source("buoy", buoy_id)
    if sensor is None:
        targets = list(b["sensors"].values()) + list(b["field_metadata"].values())
        b["contact_simulated"] = True
    elif sensor == "position":
        targets = [
            b["field_metadata"][k] for k in ("lat", "lng", "satellites", "signal_dbm")
        ]
    elif sensor in b["field_metadata"]:
        targets = [b["field_metadata"][sensor]]
    else:
        targets = [b["sensors"][sensor]]
    for target in targets:
        target["simulated"] = True
    b["battery_simulated"] = b["field_metadata"]["battery"]["simulated"]
    b["position_simulated"] = all(
        b["field_metadata"][k]["simulated"]
        for k in ("lat", "lng", "satellites", "signal_dbm")
    )
    return b


@mutation
def set_sensor_rules(buoy_id, sensor, rules):
    s = _source("buoy", buoy_id)["sensors"][sensor]
    s["rules"] = rules
    _evaluate()
    return rules


@mutation
def update_buoy_settings(buoy_id, values):
    b = _source("buoy", buoy_id)
    b.update({k: v for k, v in values.items() if v is not None})
    _evaluate()
    return b


@mutation
def update_incident(incident_id, values):
    inc = _state["incidents"][incident_id]
    values = {k: v for k, v in values.items() if v is not None}
    if values.get("status") == "ongoing":
        if any(
            i["id"] != incident_id
            and i["source_type"] == inc["source_type"]
            and i["source"] == inc["source"]
            and i["status"] == "ongoing"
            for i in _state["incidents"].values()
        ):
            raise ValueError("this source already has an ongoing incident")
    if "acknowledged" in values:
        inc["acknowledged_at"] = (
            utcnow().isoformat() if values.pop("acknowledged") else None
        )
    if "status" in values:
        inc["resolved_at"] = (
            utcnow().isoformat() if values["status"] == "resolved" else None
        )
    changed = [key for key, value in values.items() if inc.get(key) != value]
    inc.update(values)
    if changed:
        event = _alert(
            "info",
            inc["source_type"],
            inc["source"],
            "Operator updated: " + ", ".join(changed),
        )
        inc["events"].append(event)
    return inc


@mutation
def add_incident_note(incident_id, values):
    inc = _state["incidents"][incident_id]
    note = dict(values, id=uuid.uuid4().hex, time=utcnow().isoformat())
    inc["notes"].append(note)
    return note


@mutation
def acknowledge_alert(alert_id, acknowledged):
    found = None
    for alert in _state["alerts"]:
        if alert["id"] == alert_id:
            alert["acknowledged_at"] = utcnow().isoformat() if acknowledged else None
            found = alert
    if found is None:
        raise KeyError(alert_id)
    for inc in _state["incidents"].values():
        for event in inc["events"]:
            if event["id"] == alert_id:
                event["acknowledged_at"] = found["acknowledged_at"]
    return found


@mutation
def add_mute(kind, source, duration_minutes):
    _source(kind, source)
    mid = f"{kind}:{source}"
    mute = {
        "id": mid,
        "source_type": kind,
        "source": source,
        "expires_at": _iso(time.time() + duration_minutes * 60),
    }
    _state["mutes"][mid] = mute
    return mute


@mutation
def delete_mute(mute_id):
    if mute_id not in _state["mutes"]:
        raise KeyError(mute_id)
    del _state["mutes"][mute_id]


@mutation
def add_maintenance(buoy_id, values):
    _source("buoy", buoy_id)
    event = dict(
        values,
        id=uuid.uuid4().hex,
        buoy_id=buoy_id,
        time=_parse_time(values.get("time")).isoformat(),
        recorded_at=utcnow().isoformat(),
    )
    _state["maintenance"].append(event)
    return event


def get_mutes():
    with _lock:
        return copy.deepcopy(
            [
                m
                for m in _state["mutes"].values()
                if _parse_time(m["expires_at"]) > utcnow()
            ]
        )


def get_maintenance(buoy_id):
    with _lock:
        _source("buoy", buoy_id)
        return copy.deepcopy(
            sorted(
                [m for m in _state["maintenance"] if m["buoy_id"] == buoy_id],
                key=lambda m: m["time"],
                reverse=True,
            )
        )


def get_alerts(limit=20):
    with _lock:
        muted = {m["id"] for m in get_mutes()}
        return [
            dict(copy.deepcopy(a), muted=f"{a['source_type']}:{a['source']}" in muted)
            for a in reversed(_state["alerts"][-limit:])
        ]


def get_incidents():
    with _lock:
        return copy.deepcopy(
            sorted(
                _state["incidents"].values(),
                key=lambda i: i["started_at"],
                reverse=True,
            )
        )


def get_incident(incident_id):
    with _lock:
        return copy.deepcopy(_state["incidents"][incident_id])


def get_all_service_states():
    with _lock:
        return copy.deepcopy(_state["services"])


def get_service_state(name):
    with _lock:
        return copy.deepcopy(_state["services"].get(name))


def get_all_buoy_states():
    with _lock:
        return copy.deepcopy(_state["buoys"])


def get_buoy_state(buoy_id):
    with _lock:
        return copy.deepcopy(_state["buoys"].get(buoy_id))


def seconds_since_start():
    return time.time() - _start_time


def flush_state():
    with _lock:
        state_store.save(_state)
