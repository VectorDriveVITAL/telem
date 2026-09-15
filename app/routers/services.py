from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from .. import generator
from ..influx_client import query_history, query_timeseries
from ..models import ServiceCreate, ServiceStatus

router = APIRouter(prefix="/api/services", tags=["services"])


def _to_status(state: dict) -> ServiceStatus:
    return ServiceStatus(
        name=state["name"],
        status=generator._service_health(state)[0],
        latency_ms=round(state["latency"], 1),
        rps=round(state["rps"]),
        error_rate=round(state["error_rate"], 3),
        simulated=state["simulated"],
        last_reading_at=state.get("last_reading_at"),
        stale=generator._age(state.get("last_reading_at")) > 300,
        warn_ms=state["warn_ms"],
        crit_ms=state["crit_ms"],
        latency_delta_ms=state.get("latency_delta_ms", 0),
    )


@router.get("", response_model=list[ServiceStatus], summary="List all services")
def list_services():
    """Current status for every registered service - simulated ones included."""
    return [_to_status(s) for s in generator.get_all_service_states().values()]


@router.post(
    "", response_model=ServiceStatus, status_code=201, summary="Register a new service"
)
def create_service(body: ServiceCreate):
    """Adds a new service to the fleet, starting out simulated. 409 if the name is already taken."""
    try:
        state = generator.register_service(body.name, body.warn_ms, body.crit_ms)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _to_status(state)


@router.delete("/{name}", status_code=204, summary="Remove a service")
def delete_service(name: str = Path(..., examples=["calibration-worker"])):
    """Deregisters a service. Its historical data in InfluxDB is left alone."""
    try:
        generator.deregister_service(name)
    except KeyError:
        raise HTTPException(404, f"unknown service: {name}")


@router.post(
    "/{name}/simulate",
    response_model=ServiceStatus,
    summary="Resume simulating a service",
)
def resume_simulation(name: str = Path(..., examples=["calibration-worker"])):
    """Flips a service back to simulated, picking up its random walk from the current value."""
    try:
        state = generator.resume_service_simulation(name)
    except KeyError:
        raise HTTPException(404, f"unknown service: {name}")
    return _to_status(state)


@router.get(
    "/{name}/timeseries",
    summary="Service history; legacy latency array or aligned metrics",
)
def service_timeseries(
    name: str,
    minutes: int = Query(60, ge=5, le=44640),
    metric: str = "latency_ms",
    metrics: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    aggregation: Literal["raw", "mean", "min", "max", "last"] = "mean",
    interval_seconds: int | None = Query(None, ge=1, le=86400),
):
    if not generator.get_service_state(name):
        raise HTTPException(404, "unknown service")
    fields = list(dict.fromkeys(metrics.split(","))) if metrics else [metric]
    if not fields or any(f not in {"latency_ms", "rps", "error_rate"} for f in fields):
        raise HTTPException(400, "unknown service metric")
    query = query_history if metrics else query_timeseries
    return query(
        "service_metrics",
        "service",
        name,
        fields if metrics else metric,
        minutes,
        start,
        end,
        aggregation,
        interval_seconds,
    )
