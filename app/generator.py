"""
Drives the whole mock fleet.

This is deliberately NOT a script that plays back a fixed, scripted story —
it's a live simulation. Each service/buoy metric does a bounded random walk
(mean-reverting, so it wanders but doesn't drift off to infinity), gets
written to InfluxDB on every tick, and alerts/incidents are derived
server-side from real threshold crossings on that data — an alert fires when
a metric actually crosses into warning/critical, and resolves when it comes
back down. Restart the server and you'll get a different (but similarly
shaped) story each time, the same way a real fleet would never replay
identically.
"""
import asyncio
import random
import time
from datetime import timedelta

from .config import settings
from .influx_client import make_point, write_points, utcnow
from .seed_data import SERVICES, BUOYS, METRIC_RANGES

MOORING_LAT = sum(b["lat"] for b in BUOYS) / len(BUOYS)
MOORING_LNG = sum(b["lng"] for b in BUOYS) / len(BUOYS)

# ---------------------------------------------------------------- in-memory state
# InfluxDB is the source of truth for the time-series values themselves;
# this in-memory state is just what the generator needs to step the random
# walk forward each tick, plus the derived alert/incident feed (which is
# event-driven and doesn't really belong in a time-series bucket).

_service_state: dict[str, dict] = {}
_buoy_state: dict[str, dict] = {}
_alerts: list[dict] = []
_incidents: dict[str, dict] = {}   # keyed by source name, only one *open* incident per source
_incident_counter = 10             # first generated incident is INC-011, so it reads naturally
                                    # alongside a handful of pre-existing ones on a fresh boot
_start_time = time.time()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _mean_revert_step(value, base, volatility, reversion=0.06, bias=0.0):
    delta = reversion * (base - value) + (random.random() - 0.5) * volatility
    if bias and random.random() < 0.15:
        delta += bias * random.uniform(0.5, 1.5)
    return value + delta


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


def _buoy_status(buoy_id, battery, turbidity, cfg):
    if battery <= 15:
        return "critical", "low battery"
    if cfg.get("turbidity_drift") and turbidity >= 8:
        return "warning", "sensor fault"
    if cfg.get("gnss_degraded"):
        return "warning", "GNSS degraded"
    return "healthy", "healthy"


def _handle_transition(source_type, source, old_status, new_status, detail_message):
    """Edge-triggered alerting: only fires when status actually changes, and
    opens/closes an incident to match — this is what makes the Incidents tab
    reflect genuine threshold crossings instead of a canned script."""
    if old_status == new_status:
        return
    severity_rank = {"healthy": 0, "warning": 1, "critical": 2}
    getting_worse = severity_rank[new_status] > severity_rank[old_status]

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
    _ = getting_worse  # reserved for future use (e.g. escalation-only notifications)


def _guess_cause(source_type: str, source: str) -> str:
    if source_type == "service":
        return f"Correlates with recent load on {source} - no confirmed root cause yet."
    return f"Sensor or hardware condition on {source} - no confirmed root cause yet."


# ---------------------------------------------------------------- seeding

def _init_service_state():
    for svc in SERVICES:
        _service_state[svc["name"]] = {
            "latency": svc["base_latency_ms"],
            "rps": svc["base_rps"],
            "error_rate": round(random.uniform(0.0, 0.05), 3),
            "status": "healthy",
        }


def _init_buoy_state():
    for b in BUOYS:
        state = {
            **{k: v for k, v in b["base"].items()},
            "battery": b["base_battery"],
            "satellites": random.randint(6, 9),
            "status": "healthy",
            "last_contact": time.time(),
        }
        # Signal strength: weaker with distance from the mooring anchor, plus
        # a bit of noise - not scientifically modeled, but at least tied to
        # something real about the deployment rather than pure random jitter.
        dist = ((b["lat"] - MOORING_LAT) ** 2 + (b["lng"] - MOORING_LNG) ** 2) ** 0.5
        state["signal_dbm"] = round(-58 - dist * 2200 + random.uniform(-4, 4))
        _buoy_state[b["id"]] = state


def seed_history():
    """Backfill ~history_minutes of 1-minute-resolution data so charts have
    something to show the moment the dashboard loads, instead of an empty
    graph that only fills in live over the next few hours."""
    _init_service_state()
    _init_buoy_state()

    minutes = settings.history_minutes
    now = utcnow()
    points = []

    for svc in SERVICES:
        v = svc["base_latency_ms"]
        rps = svc["base_rps"]
        for i in range(minutes, 0, -1):
            v = _clamp(_mean_revert_step(v, svc["base_latency_ms"], svc["volatility"], bias=svc["bias"]), 10, 1500)
            rps = _clamp(rps + (random.random() - 0.5) * (svc["base_rps"] * 0.05), 1, svc["base_rps"] * 3)
            err = _clamp(random.random() * (2.0 if v > svc["crit_ms"] else 0.3), 0, 5)
            t = now - timedelta(minutes=i)
            points.append(make_point(
                "service_metrics", {"service": svc["name"]},
                {"latency_ms": v, "rps": rps, "error_rate": err}, time=t,
            ))
        _service_state[svc["name"]]["latency"] = v
        _service_state[svc["name"]]["rps"] = rps

    for b in BUOYS:
        vals = dict(b["base"])
        battery = b["base_battery"]
        for i in range(minutes, 0, -1):
            for metric, vol in b["volatility"].items():
                lo, hi = METRIC_RANGES[metric]
                drift = b.get("turbidity_drift", 0) if metric == "turbidity" else 0
                vals[metric] = _clamp(
                    _mean_revert_step(vals[metric], b["base"][metric], vol) + drift, lo, hi
                )
            if b.get("battery_drain"):
                battery = _clamp(battery - b["battery_drain"] * random.uniform(0.5, 1.5), 3, 100)
            else:
                # gentle charge/discharge cycle so battery isn't perfectly flat
                battery = _clamp(battery + (random.random() - 0.45) * 0.4, 20, 100)
            t = now - timedelta(minutes=i)
            points.append(make_point(
                "buoy_metrics", {"buoy": b["id"]},
                {**vals, "battery": battery}, time=t,
            ))
        _buoy_state[b["id"]].update(vals)
        _buoy_state[b["id"]]["battery"] = battery

    write_points(points)


# ---------------------------------------------------------------- live ticking

def _tick_once():
    now = utcnow()
    points = []

    for svc in SERVICES:
        state = _service_state[svc["name"]]
        old_status = state["status"]
        state["latency"] = _clamp(
            _mean_revert_step(state["latency"], svc["base_latency_ms"], svc["volatility"], bias=svc["bias"]),
            10, 1500,
        )
        state["rps"] = _clamp(state["rps"] + (random.random() - 0.5) * (svc["base_rps"] * 0.05), 1, svc["base_rps"] * 3)
        state["error_rate"] = _clamp(random.random() * (2.0 if state["latency"] > svc["crit_ms"] else 0.3), 0, 5)
        new_status = _service_status(state["latency"], svc)
        state["status"] = new_status
        detail = f"{svc['name']} - p95 latency at {state['latency']:.0f}ms"
        _handle_transition("service", svc["name"], old_status, new_status, detail)
        points.append(make_point(
            "service_metrics", {"service": svc["name"]},
            {"latency_ms": state["latency"], "rps": state["rps"], "error_rate": state["error_rate"]}, time=now,
        ))

    for b in BUOYS:
        state = _buoy_state[b["id"]]
        old_status = state["status"]
        for metric, vol in b["volatility"].items():
            lo, hi = METRIC_RANGES[metric]
            drift = b.get("turbidity_drift", 0) if metric == "turbidity" else 0
            state[metric] = _clamp(_mean_revert_step(state[metric], b["base"][metric], vol) + drift, lo, hi)
        if b.get("battery_drain"):
            state["battery"] = _clamp(state["battery"] - b["battery_drain"] * random.uniform(0.5, 1.5), 3, 100)
        else:
            state["battery"] = _clamp(state["battery"] + (random.random() - 0.45) * 0.4, 20, 100)
        state["last_contact"] = time.time() if random.random() > 0.05 else state["last_contact"]  # occasional missed check-in
        dist = ((b["lat"] - MOORING_LAT) ** 2 + (b["lng"] - MOORING_LNG) ** 2) ** 0.5
        state["signal_dbm"] = round(-58 - dist * 2200 + random.uniform(-4, 4))

        new_status, status_text = _buoy_status(b["id"], state["battery"], state["turbidity"], b)
        state["status"] = new_status
        state["status_text"] = status_text
        if new_status != old_status:
            detail = f"{b['id']} - {status_text}"
            _handle_transition("buoy", b["id"], old_status, new_status, detail)
        points.append(make_point(
            "buoy_metrics", {"buoy": b["id"]},
            {k: state[k] for k in ("temp", "salinity", "turbidity", "ph", "battery")}, time=now,
        ))

    write_points(points)


async def background_loop():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _tick_once)
        except Exception as e:  # pragma: no cover - keep the simulator alive even if a write hiccups
            print(f"[generator] tick failed: {e}")
        await asyncio.sleep(settings.tick_seconds)


# ---------------------------------------------------------------- accessors used by routers

def get_service_state(name: str) -> dict | None:
    return _service_state.get(name)


def get_all_service_states() -> dict[str, dict]:
    return _service_state


def get_buoy_state(buoy_id: str) -> dict | None:
    return _buoy_state.get(buoy_id)


def get_all_buoy_states() -> dict[str, dict]:
    return _buoy_state


def get_alerts(limit: int = 20) -> list[dict]:
    return list(reversed(_alerts[-limit:]))


def get_incidents() -> list[dict]:
    return sorted(_incidents.values(), key=lambda i: i["started_at"], reverse=True)


def seconds_since_start() -> float:
    return time.time() - _start_time
