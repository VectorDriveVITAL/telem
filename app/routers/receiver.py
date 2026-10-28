"""Control of the serial bridge and the board's RX/TX role."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import receiver
from ..security import control

router = APIRouter(prefix="/api/receiver", tags=["receiver"])
# Anything that starts processes, flashes hardware or edits settings goes through the guard.
guarded = [Depends(control)]


class ModeChange(BaseModel):
    mode: Literal["rx", "tx"]


class StartOptions(BaseModel):
    dry_run: Optional[bool] = None  # None keeps the previous choice
    port: Optional[str] = None  # None keeps it, "" means auto-detect


class AlertChoices(BaseModel):
    link: Optional[bool] = None
    hull: Optional[bool] = None
    stuck: Optional[bool] = None


def _self_url(request: Request):
    # The bridge runs on this host, so it can always reach us over loopback.
    return f"http://127.0.0.1:{request.url.port or 80}"


@router.get("", summary="Bridge state, link health, board role and recent bridge log")
def status(since: Optional[int] = None):
    """Pass the `log_total` from a previous response as `since` to receive only newer log lines."""
    return receiver.status(since)


@router.get("/stats", summary="Link analytics (delivery ratio, margins, histograms) and pipeline health")
def stats():
    return receiver.stats()


@router.get("/packets", summary="Recent stored packets, newest first")
def packets(seq: Optional[int] = None, limit: int = Query(100, ge=1, le=1000)):
    return receiver.packets(seq, limit)


@router.get("/export", summary="Download the bridge log or the stored packets as a file")
def export(kind: Literal["log", "packets"] = "log"):
    if kind == "packets":
        body, name, media = receiver.export_packets_csv(), "packets.csv", "text/csv"
    else:
        body, name, media = receiver.export_log_text(), "bridge.log", "text/plain"
    return PlainTextResponse(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/start", summary="Launch the bridge and keep it running", dependencies=guarded)
def start(request: Request, body: StartOptions = StartOptions()):
    return receiver.start(_self_url(request), body.dry_run, body.port)


@router.post("/stop", summary="Stop the bridge and leave it stopped", dependencies=guarded)
def stop():
    return receiver.stop()


@router.post("/restart", summary="Stop and relaunch the bridge", dependencies=guarded)
def restart(request: Request, body: StartOptions = StartOptions()):
    return receiver.restart(_self_url(request), body.dry_run, body.port)


@router.delete("/log", summary="Clear the bridge log, including the saved log file", dependencies=guarded)
def clear_log():
    return receiver.clear_log()


@router.put("/mode", summary="Flash the board as RX or TX (takes up to a minute)", dependencies=guarded)
def set_mode(body: ModeChange, request: Request):
    return receiver.set_mode(body.mode, _self_url(request))


@router.put("/alerts", summary="Choose which analytics may raise telem alerts", dependencies=guarded)
def set_alerts(body: AlertChoices):
    receiver.set_alerts(**body.model_dump())
    return receiver.status()
