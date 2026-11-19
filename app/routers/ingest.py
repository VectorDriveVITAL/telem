import math
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Path, Request
from pydantic import ValidationError

from .. import generator
from ..models import (
    BuoyHeartbeat,
    BuoyIngestReading,
    BuoyStatus,
    ServiceIngestReading,
    ServiceStatus,
)
from .buoys import _to_status as _buoy_to_status
from .services import _to_status as _service_to_status

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.post(
    "/services/{name}",
    response_model=ServiceStatus,
    summary="Ingest one reading for a service",
)
def ingest_service(
    name: str = Path(..., examples=["telemetry-gateway"]),
    body: ServiceIngestReading = ...,
):
    """Send any subset of latency_ms/rps/error_rate. Each field you send flips that service to
    real (`simulated: false`) and the background simulator stops touching it. 404 if the service
    isn't registered yet - see POST /api/services."""
    try:
        state = generator.ingest_service(
            name, body.latency_ms, body.rps, body.error_rate, body.time
        )
    except KeyError:
        raise HTTPException(
            404, f"unknown service: {name} - register it first with POST /api/services"
        )
    return _service_to_status(state)


@router.post(
    "/services/{name}/batch",
    response_model=ServiceStatus,
    summary="Ingest a batch of readings for a service",
)
def ingest_service_batch(
    name: str = Path(..., examples=["telemetry-gateway"]),
    body: list[ServiceIngestReading] = Body(..., min_length=1, max_length=1000),
):
    """Same as the single-reading endpoint, but for backfill or a batched uplink - an array of
    readings, each optionally with its own `time`."""
    if not generator.get_service_state(name):
        raise HTTPException(
            404, f"unknown service: {name} - register it first with POST /api/services"
        )
    generator.ingest_service_batch(name, [r.model_dump() for r in body])
    return _service_to_status(generator.get_service_state(name))


def _buoy_reading_dict(body: BuoyIngestReading) -> dict:
    d = body.model_dump(exclude={"time"})
    d.update(body.model_extra or {})
    return d


@router.post(
    "/buoys/{buoy_id}",
    response_model=BuoyStatus,
    summary="Ingest one reading for a buoy",
)
def ingest_buoy(
    buoy_id: str = Path(..., examples=["buoy-01"]), body: BuoyIngestReading = ...
):
    """Send battery/lat/lng/satellites/signal_dbm and/or any sensor name as a field, e.g.
    `{"temp": 18.4, "turbidity": 3.1}`. A field name that isn't a known sensor on this buoy
    yet is auto-registered - no need to call POST /api/buoys/{buoy_id}/sensors first. Each
    field flips independently from simulated to real. 404 if the buoy isn't registered -
    see POST /api/buoys."""
    try:
        state = generator.ingest_buoy(buoy_id, _buoy_reading_dict(body), body.time)
    except KeyError:
        raise HTTPException(
            404, f"unknown buoy: {buoy_id} - register it first with POST /api/buoys"
        )
    return _buoy_to_status(state)


@router.post(
    "/buoys/{buoy_id}/batch",
    response_model=BuoyStatus,
    summary="Ingest a batch of readings for a buoy",
)
def ingest_buoy_batch(
    buoy_id: str = Path(..., examples=["buoy-01"]),
    body: list[BuoyIngestReading] = Body(..., min_length=1, max_length=1000),
):
    """Same as the single-reading endpoint, but for backfill or a batched uplink - useful for a
    buoy on satellite/cellular that stores readings locally and dumps a batch on reconnect."""
    if not generator.get_buoy_state(buoy_id):
        raise HTTPException(
            404, f"unknown buoy: {buoy_id} - register it first with POST /api/buoys"
        )
    readings = []
    for r in body:
        d = _buoy_reading_dict(r)
        d["time"] = r.time
        readings.append(d)
    generator.ingest_buoy_batch(buoy_id, readings)
    return _buoy_to_status(generator.get_buoy_state(buoy_id))


@router.post(
    "/buoys/{buoy_id}/heartbeat",
    response_model=BuoyStatus,
    summary="Buoy check-in with no sensor data",
)
def heartbeat(
    buoy_id: str = Path(..., examples=["buoy-01"]),
    body: BuoyHeartbeat = BuoyHeartbeat(),
):
    """Updates last-contact (and optionally signal strength) only - doesn't touch sensor
    readings or battery. For a device that checks in more often than it takes real measurements."""
    try:
        state = generator.buoy_heartbeat(buoy_id, body.signal_dbm)
    except KeyError:
        raise HTTPException(
            404, f"unknown buoy: {buoy_id} - register it first with POST /api/buoys"
        )
    return _buoy_to_status(state)


# MQTT topic plan (see pi/TOPICS.md): the source id is read from the topic, so adding a buoy
# needs no Telegraf change.
_TOPIC = re.compile(r"^vital/(buoy|service)/([^/]+)/(?:readings|metrics)$")
_SERVICE_FIELDS = ("latency_ms", "rps", "error_rate")


def _epoch_to_iso(ts):
    """Telegraf's JSON timestamp is an integer in whatever unit json_timestamp_units says."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        raise ValueError("timestamp must be a number")
    for limit, divisor in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):  # ns, us, ms; below is seconds
        if abs(ts) >= limit:
            ts /= divisor
            break
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _ingest_telegraf_metric(metric):
    match = _TOPIC.match(str((metric.get("tags") or {}).get("topic", "")))
    if not match:
        raise ValueError("topic is not vital/buoy/<id>/readings or vital/service/<name>/metrics")
    kind, source = match.groups()
    fields = {
        k: v
        for k, v in (metric.get("fields") or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    }
    raw = (metric.get("fields") or {}).get("time")
    if isinstance(raw, str):  # the device's own time, passed through by json_string_fields
        when = raw
    else:  # otherwise when Telegraf received the message
        when = None if metric.get("timestamp") is None else _epoch_to_iso(metric["timestamp"])
    if kind == "service":
        fields = {k: v for k, v in fields.items() if k in _SERVICE_FIELDS}
    if not fields:
        raise ValueError("no numeric fields")
    if kind == "buoy":
        body = BuoyIngestReading(time=when, **fields)  # the same validation as the buoy route
        generator.ingest_buoy(source, _buoy_reading_dict(body), body.time)
    else:
        body = ServiceIngestReading(time=when, **fields)
        generator.ingest_service(source, body.latency_ms, body.rps, body.error_rate, body.time)


@router.post("/telegraf", summary="Ingest metrics forwarded by Telegraf from MQTT")
async def ingest_telegraf(request: Request):
    """Accepts Telegraf's JSON output (`{"metrics": [...]}`, a list, or one metric). Each metric
    is judged on its own: a bad one is reported under `rejected` and never blocks the rest, so
    Telegraf does not retry a batch forever. Only a storage failure (503) makes it retry."""
    payload = await request.json()
    metrics = payload.get("metrics") if isinstance(payload, dict) and "metrics" in payload else payload
    if isinstance(metrics, dict):
        metrics = [metrics]
    if not isinstance(metrics, list):
        raise ValueError("expected a Telegraf JSON metric, a list of them, or {\"metrics\": [...]}")
    rejected = []
    for i, metric in enumerate(metrics):
        try:
            if not isinstance(metric, dict):
                raise ValueError("metric must be an object")
            _ingest_telegraf_metric(metric)
        except (ValueError, KeyError) as exc:  # pydantic's ValidationError is a ValueError
            if isinstance(exc, KeyError):
                reason = f"unknown source - register it first ({exc.args[0]})"
            elif isinstance(exc, ValidationError):
                e = exc.errors()[0]
                reason = f"{'.'.join(map(str, e['loc'])) or 'reading'}: {e['msg']}"
            else:
                reason = str(exc)
            rejected.append({"index": i, "reason": reason})
            print(f"[ingest] telegraf metric {i} rejected: {reason}")
    return {"accepted": len(metrics) - len(rejected), "rejected": rejected}
