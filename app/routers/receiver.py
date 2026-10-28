"""Control of the serial bridge and the board's RX/TX role."""

from typing import Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import receiver

router = APIRouter(prefix="/api/receiver", tags=["receiver"])


class ModeChange(BaseModel):
    mode: Literal["rx", "tx"]


class StartOptions(BaseModel):
    dry_run: Optional[bool] = None  # None keeps the previous choice
    port: Optional[str] = None  # None keeps it, "" means auto-detect


def _self_url(request: Request):
    # The bridge runs on this host, so it can always reach us over loopback.
    return f"http://127.0.0.1:{request.url.port or 80}"


@router.get("", summary="Bridge state, link health, board role and recent bridge log")
def status():
    return receiver.status()


@router.post("/start", summary="Launch the bridge")
def start(request: Request, body: StartOptions = StartOptions()):
    return receiver.start(_self_url(request), body.dry_run, body.port)


@router.post("/stop", summary="Stop the bridge")
def stop():
    return receiver.stop()


@router.post("/restart", summary="Stop and relaunch the bridge")
def restart(request: Request, body: StartOptions = StartOptions()):
    return receiver.restart(_self_url(request), body.dry_run, body.port)


@router.delete("/log", summary="Clear the in-memory bridge log (the log file is kept)")
def clear_log():
    return receiver.clear_log()


@router.put("/mode", summary="Flash the board as RX or TX (takes up to a minute)")
def set_mode(body: ModeChange, request: Request):
    return receiver.set_mode(body.mode, _self_url(request))
