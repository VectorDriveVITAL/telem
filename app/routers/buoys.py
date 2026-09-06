import time

from fastapi import APIRouter, HTTPException

from .. import generator
from ..influx_client import query_timeseries
from ..models import BuoyStatus, TimeseriesPoint
from ..seed_data import BUOYS

router = APIRouter(prefix="/api/buoys", tags=["buoys"])

_BY_ID = {b["id"]: b for b in BUOYS}
_VALID_METRICS = {"temp", "salinity", "turbidity", "ph"}


@router.get("", response_model=list[BuoyStatus])
def list_buoys():
    out = []
    for cfg in BUOYS:
        state = generator.get_buoy_state(cfg["id"])
        if not state:
            continue
        gnss_fix = "no fix (last known)" if cfg.get("gnss_degraded") else f"3D - {state['satellites']} satellites"
        out.append(BuoyStatus(
            id=cfg["id"],
            status=state["status"],
            status_text=state.get("status_text", "healthy"),
            lat=cfg["lat"], lng=cfg["lng"],
            battery=round(state["battery"], 1),
            solar_watts=cfg["solar_watts"],
            temp=round(state["temp"], 2),
            salinity=round(state["salinity"], 2),
            turbidity=round(state["turbidity"], 2),
            ph=round(state["ph"], 2),
            gnss_fix=gnss_fix,
            signal_dbm=state["signal_dbm"],
            last_contact_seconds=round(time.time() - state["last_contact"], 1),
        ))
    return out


@router.get("/{buoy_id}/timeseries", response_model=list[TimeseriesPoint])
def buoy_timeseries(buoy_id: str, metric: str = "temp", minutes: int = 60):
    if buoy_id not in _BY_ID:
        raise HTTPException(404, f"unknown buoy: {buoy_id}")
    if metric not in _VALID_METRICS:
        raise HTTPException(400, f"metric must be one of {sorted(_VALID_METRICS)}")
    minutes = max(5, min(minutes, 1440))
    return query_timeseries("buoy_metrics", "buoy", buoy_id, metric, minutes)
