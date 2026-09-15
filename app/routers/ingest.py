from fastapi import APIRouter, Body, HTTPException, Path

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
