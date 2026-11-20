"""Radio link health for LoRaWAN devices (the readings themselves go through /api/ingest)."""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import lorawan

router = APIRouter(prefix="/api/lorawan", tags=["lorawan"])


class Uplink(BaseModel):
    dev_eui: str = Field(min_length=1, max_length=32)
    buoy: Optional[str] = None  # None: the device is not in the adapter's devices file yet
    f_cnt: Optional[int] = Field(None, ge=0)
    rssi: Optional[float] = None
    snr: Optional[float] = None


@router.post("/uplink", summary="Record one uplink's link quality (sent by the LoRaWAN adapter)")
def uplink(body: Uplink):
    lorawan.record(body.dev_eui.lower(), body.buoy, body.f_cnt, body.rssi, body.snr)
    return {"ok": True}


@router.get("", summary="Per-device link health and the most recent uplinks")
def status():
    return lorawan.status()
