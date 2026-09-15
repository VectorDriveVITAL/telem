"""Operator workflows, overview and bounded CSV exports."""

import csv
import io
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .. import generator as fleet
from ..influx_client import query_rows, window
from ..models import (
    AlertUpdate,
    BuoySettings,
    Incident,
    IncidentUpdate,
    MaintenanceCreate,
    MuteCreate,
    NoteCreate,
    SensorRules,
)
from .buoys import _to_status as buoy_status
from .services import _to_status as service_status

router = APIRouter(prefix="/api", tags=["operations"])


@router.get("/overview")
def overview():
    buoys = [buoy_status(b).model_dump() for b in fleet.get_all_buoy_states().values()]
    services = [
        service_status(s).model_dump() for s in fleet.get_all_service_states().values()
    ]
    incidents = fleet.get_incidents()
    rising = [
        s["name"]
        for s in services
        if not s["stale"] and s["latency_delta_ms"] > max(5, s["latency_ms"] * 0.05)
    ]
    urgent = []
    for b in buoys:
        if b["status"] != "healthy":
            urgent.append(
                {
                    "source_type": "buoy",
                    "source": b["id"],
                    "severity": b["status"],
                    "reason": b["status_text"],
                    "offline": b["offline"],
                    "low_battery": b["battery"] <= 15,
                    "stale_sensors": b["stale_sensor_count"],
                }
            )
    for s in services:
        if s["status"] != "healthy" or s["name"] in rising:
            urgent.append(
                {
                    "source_type": "service",
                    "source": s["name"],
                    "severity": s["status"],
                    "reason": "Stale reading"
                    if s["stale"]
                    else "Latency threshold exceeded"
                    if s["status"] != "healthy"
                    else "Latency rising",
                    "rising": s["name"] in rising,
                }
            )
    urgent.sort(
        key=lambda item: (
            {"critical": 0, "warning": 1, "healthy": 2}[item["severity"]],
            item["source"],
        )
    )
    return {
        "generated_at": fleet.utcnow().isoformat(),
        "counts": {
            "buoys": len(buoys),
            "services": len(services),
            "offline_buoys": sum(b["offline"] for b in buoys),
            "low_batteries": sum(b["battery"] <= 15 for b in buoys),
            "stale_sensors": sum(b["stale_sensor_count"] for b in buoys),
            "active_incidents": sum(i["status"] == "ongoing" for i in incidents),
            "rising_services": len(rising),
        },
        "rising_services": rising,
        "urgent_sources": urgent,
    }


@router.get("/buoys/{buoy_id}/sensors/{sensor}/rules", response_model=SensorRules)
def get_rules(buoy_id: str, sensor: str):
    b = fleet.get_buoy_state(buoy_id)
    if not b or sensor not in b["sensors"]:
        raise HTTPException(404, "unknown buoy or sensor")
    return b["sensors"][sensor]["rules"]


@router.put("/buoys/{buoy_id}/sensors/{sensor}/rules", response_model=SensorRules)
def put_rules(buoy_id: str, sensor: str, body: SensorRules):
    return fleet.set_sensor_rules(buoy_id, sensor, body.model_dump())


@router.put("/buoys/{buoy_id}/settings")
def put_settings(buoy_id: str, body: BuoySettings):
    return buoy_status(fleet.update_buoy_settings(buoy_id, body.model_dump()))


@router.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: str):
    return fleet.get_incident(incident_id)


@router.patch("/incidents/{incident_id}", response_model=Incident)
def patch_incident(incident_id: str, body: IncidentUpdate):
    return fleet.update_incident(incident_id, body.model_dump(exclude_unset=True))


@router.post("/incidents/{incident_id}/notes", status_code=201)
def add_note(incident_id: str, body: NoteCreate):
    return fleet.add_incident_note(incident_id, body.model_dump())


@router.patch("/alerts/{alert_id}")
def patch_alert(alert_id: str, body: AlertUpdate):
    return fleet.acknowledge_alert(alert_id, body.acknowledged)


@router.get("/mutes")
def mutes():
    return fleet.get_mutes()


@router.post("/mutes", status_code=201)
def mute(body: MuteCreate):
    return fleet.add_mute(body.source_type, body.source, body.duration_minutes)


@router.delete("/mutes/{mute_id}", status_code=204)
def unmute(mute_id: str):
    fleet.delete_mute(mute_id)


@router.get("/buoys/{buoy_id}/maintenance")
def maintenance(buoy_id: str):
    return fleet.get_maintenance(buoy_id)


@router.post("/buoys/{buoy_id}/maintenance", status_code=201)
def add_maintenance(buoy_id: str, body: MaintenanceCreate):
    return fleet.add_maintenance(buoy_id, body.model_dump())


def _csv(headers, rows, filename):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        # Keep freeform notes/names from becoming formulas when opened in Excel.
        writer.writerow(
            [
                "'" + v
                if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                else v
                for v in row
            ]
        )
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export", summary="Export selected readings or incident history as CSV")
def export(
    kind: Literal["buoy", "service", "incidents"] = "buoy",
    source: str | None = None,
    metrics: str | None = None,
    minutes: int = Query(60, ge=5, le=44640),
    start: datetime | None = None,
    end: datetime | None = None,
    aggregation: Literal["raw", "mean", "min", "max", "last"] = "raw",
    interval_seconds: int = Query(60, ge=1, le=86400),
):
    start, end = window(minutes, start, end)
    if kind == "incidents":
        items = [
            i
            for i in fleet.get_incidents()
            if start <= fleet._parse_time(i["started_at"]) < end
            and (not source or i["source"] == source)
        ]
        headers = [
            "id",
            "source_type",
            "source",
            "title",
            "severity",
            "status",
            "started_at",
            "resolved_at",
            "owner",
            "acknowledged_at",
            "cause",
            "notes",
            "events",
        ]
        return _csv(
            headers,
            [
                [
                    "\n".join(
                        f"{e['time']} {e['severity']}: {e['message']}"
                        for e in i["events"]
                    )
                    if h == "events"
                    else i.get(h, "")
                    if h != "notes"
                    else "\n".join(
                        f"{n['time']} {n['author']}: {n['text']}" for n in i["notes"]
                    )
                    for h in headers
                ]
                for i in items
            ],
            "telem-incidents.csv",
        )
    if not source:
        raise HTTPException(400, "source is required for reading exports")
    if kind == "buoy":
        b = fleet.get_buoy_state(source)
        if not b:
            raise HTTPException(404, "unknown buoy")
        units = {k: v["unit"] for k, v in b["sensors"].items()} | fleet.UNITS
        measurement, tag = "buoy_metrics", "buoy"
    else:
        if not fleet.get_service_state(source):
            raise HTTPException(404, "unknown service")
        units = {"latency_ms": "ms", "rps": "req/s", "error_rate": "%"}
        measurement, tag = "service_metrics", "service"
    fields = list(dict.fromkeys(metrics.split(","))) if metrics else list(units)
    if not fields or any(f not in units for f in fields):
        raise HTTPException(400, "unknown metric")
    rows = query_rows(
        measurement, tag, source, fields, start, end, aggregation, interval_seconds
    )
    return _csv(
        ["time", "source_type", "source", "metric", "value", "unit", "aggregation"],
        [
            [
                r["time"],
                kind,
                source,
                r["metric"],
                r["value"],
                units[r["metric"]],
                aggregation,
            ]
            for r in rows
        ],
        "telem-readings.csv",
    )
