from fastapi import APIRouter, HTTPException, Path, Query

from .. import generator
from ..influx_client import query_timeseries
from ..models import ServiceStatus, TimeseriesPoint, ServiceCreate

router = APIRouter(prefix="/api/services", tags=["services"])


def _to_status(state: dict) -> ServiceStatus:
    return ServiceStatus(
        name=state["name"],
        status=state["status"],
        latency_ms=round(state["latency"], 1),
        rps=round(state["rps"]),
        error_rate=round(state["error_rate"], 3),
        simulated=state["simulated"],
    )


@router.get("", response_model=list[ServiceStatus], summary="List all services")
def list_services():
    """Current status for every registered service - simulated ones included."""
    return [_to_status(s) for s in generator.get_all_service_states().values()]


@router.post("", response_model=ServiceStatus, status_code=201, summary="Register a new service")
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


@router.post("/{name}/simulate", response_model=ServiceStatus, summary="Resume simulating a service")
def resume_simulation(name: str = Path(..., examples=["calibration-worker"])):
    """Flips a service back to simulated, picking up its random walk from the current value."""
    try:
        state = generator.resume_service_simulation(name)
    except KeyError:
        raise HTTPException(404, f"unknown service: {name}")
    return _to_status(state)


@router.get("/{name}/timeseries", response_model=list[TimeseriesPoint], summary="Get a service's latency history")
def service_timeseries(
    name: str = Path(..., examples=["telemetry-gateway"]),
    minutes: int = Query(60, description="How far back to look, in minutes (5-1440)."),
):
    """p95 latency over time, aggregated to ~60 points regardless of the window size."""
    if not generator.get_service_state(name):
        raise HTTPException(404, f"unknown service: {name}")
    minutes = max(5, min(minutes, 1440))
    return query_timeseries("service_metrics", "service", name, "latency_ms", minutes)
