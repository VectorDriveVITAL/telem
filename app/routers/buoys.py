import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from .. import generator
from ..influx_client import query_history, query_timeseries
from ..models import (
    BuoyCreate,
    BuoyStatus,
    SensorCreate,
    SensorReading,
    SimulateRequest,
)

router = APIRouter(prefix="/api/buoys", tags=["buoys"])


def _to_status(state: dict) -> BuoyStatus:
    gnss_fix = (
        f"{state['satellites']} satellites" if state["satellites"] >= 4 else "No 3D fix"
    )
    health, reasons = generator._buoy_health(state)
    return BuoyStatus(
        id=state["id"],
        status=health,
        status_text="; ".join(reasons) or "healthy",
        lat=state["lat"],
        lng=state["lng"],
        battery=round(state["battery"], 1),
        battery_simulated=state["battery_simulated"],
        solar_watts=state["solar_watts"],
        satellites=state["satellites"],
        signal_dbm=state["signal_dbm"],
        position_simulated=state["position_simulated"],
        gnss_fix=gnss_fix,
        last_contact_seconds=round(time.time() - state["last_contact"], 1),
        last_contact_at=generator._iso(state["last_contact"]),
        contact_simulated=state["contact_simulated"],
        offline=time.time() - state["last_contact"] > state["offline_after_seconds"],
        offline_after_seconds=state["offline_after_seconds"],
        stale_sensor_count=sum(
            generator._sensor_health(s)[1] for s in state["sensors"].values()
        ),
        field_metadata=state["field_metadata"],
        mooring_lat=state["mooring_lat"],
        mooring_lng=state["mooring_lng"],
        sensors={
            name: SensorReading(
                value=round(cfg["value"], 3),
                unit=cfg["unit"],
                simulated=cfg["simulated"],
                last_reading_at=cfg.get("last_reading_at"),
                stale=generator._sensor_health(cfg)[1],
                status=generator._sensor_health(cfg)[0],
                rules=cfg["rules"],
            )
            for name, cfg in state["sensors"].items()
        },
    )


@router.get("", response_model=list[BuoyStatus], summary="List all buoys")
def list_buoys():
    """Current status for every registered buoy, including its actual sensor set - heterogeneous per buoy."""
    return [_to_status(b) for b in generator.get_all_buoy_states().values()]


@router.post(
    "", response_model=BuoyStatus, status_code=201, summary="Register a new buoy"
)
def create_buoy(body: BuoyCreate):
    """Adds a new buoy. `sensors` can be an empty list - a buoy with only battery/GNSS is valid.
    409 if the id is already taken."""
    try:
        state = generator.register_buoy(
            body.id, body.lat, body.lng, sensors=body.sensors, units=body.units
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _to_status(state)


@router.delete("/{buoy_id}", status_code=204, summary="Remove a buoy")
def delete_buoy(buoy_id: str = Path(..., examples=["buoy-07"])):
    """Deregisters a buoy. Its historical data in InfluxDB is left alone."""
    try:
        generator.deregister_buoy(buoy_id)
    except KeyError:
        raise HTTPException(404, f"unknown buoy: {buoy_id}")


@router.post(
    "/{buoy_id}/sensors",
    response_model=BuoyStatus,
    status_code=201,
    summary="Add a sensor to a buoy",
)
def add_sensor(
    buoy_id: str = Path(..., examples=["buoy-07"]), body: SensorCreate = ...
):
    """Registers a sensor on an existing buoy ahead of it sending real data. 409 if it already exists -
    but note you don't need this at all if you're about to ingest data for a brand new sensor name;
    POST /api/ingest/buoys/{buoy_id} auto-registers unrecognized fields on its own."""
    try:
        generator.add_buoy_sensor(buoy_id, body.sensor, body.unit)
    except KeyError:
        raise HTTPException(404, f"unknown buoy: {buoy_id}")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _to_status(generator.get_buoy_state(buoy_id))


@router.delete(
    "/{buoy_id}/sensors/{sensor}",
    response_model=BuoyStatus,
    summary="Remove a sensor from a buoy",
)
def remove_sensor(
    buoy_id: str = Path(..., examples=["buoy-02"]),
    sensor: str = Path(..., examples=["turbidity"]),
):
    """Removes one sensor from a buoy - e.g. a probe failed and was physically pulled."""
    try:
        generator.remove_buoy_sensor(buoy_id, sensor)
    except KeyError as e:
        raise HTTPException(404, str(e) or f"unknown buoy: {buoy_id}")
    return _to_status(generator.get_buoy_state(buoy_id))


@router.post(
    "/{buoy_id}/simulate",
    response_model=BuoyStatus,
    summary="Resume simulating a buoy (or one sensor on it)",
)
def resume_simulation(
    buoy_id: str = Path(..., examples=["buoy-04"]),
    body: SimulateRequest = SimulateRequest(),
):
    """With `sensor` given ("battery", "position", or a sensor name), flips just that field back to
    simulated. Omit it (or send `{}`) to reset the whole buoy - all sensors plus battery and position."""
    try:
        state = generator.resume_buoy_simulation(buoy_id, body.sensor)
    except KeyError as e:
        raise HTTPException(404, str(e) or "unknown buoy or sensor")
    return _to_status(state)


@router.get(
    "/{buoy_id}/timeseries",
    summary="Sensor and power history; optional aligned multi-metric response",
)
def buoy_timeseries(
    buoy_id: str,
    metric: str = "temp",
    minutes: int = Query(60, ge=5, le=44640),
    metrics: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    aggregation: Literal["raw", "mean", "min", "max", "last"] = "mean",
    interval_seconds: int | None = Query(None, ge=1, le=86400),
):
    state = generator.get_buoy_state(buoy_id)
    if not state:
        raise HTTPException(404, "unknown buoy")
    fields = list(dict.fromkeys(metrics.split(","))) if metrics else [metric]
    allowed = set(state["sensors"]) | generator.KNOWN_BUOY_UNIVERSAL_FIELDS
    if not fields or any(f not in allowed for f in fields):
        raise HTTPException(400, "unknown buoy metric")
    query = query_history if metrics else query_timeseries
    return query(
        "buoy_metrics",
        "buoy",
        buoy_id,
        fields if metrics else metric,
        minutes,
        start,
        end,
        aggregation,
        interval_seconds,
    )
