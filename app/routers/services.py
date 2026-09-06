from fastapi import APIRouter, HTTPException

from .. import generator
from ..influx_client import query_timeseries
from ..models import ServiceStatus, TimeseriesPoint
from ..seed_data import SERVICES

router = APIRouter(prefix="/api/services", tags=["services"])

_NAMES = {s["name"] for s in SERVICES}


@router.get("", response_model=list[ServiceStatus])
def list_services():
    out = []
    for svc in SERVICES:
        state = generator.get_service_state(svc["name"])
        if not state:
            continue
        out.append(ServiceStatus(
            name=svc["name"],
            status=state["status"],
            latency_ms=round(state["latency"], 1),
            rps=round(state["rps"]),
            error_rate=round(state["error_rate"], 3),
        ))
    return out


@router.get("/{name}/timeseries", response_model=list[TimeseriesPoint])
def service_timeseries(name: str, minutes: int = 60):
    if name not in _NAMES:
        raise HTTPException(404, f"unknown service: {name}")
    minutes = max(5, min(minutes, 1440))
    return query_timeseries("service_metrics", "service", name, "latency_ms", minutes)
