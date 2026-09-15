from typing import Optional

from pydantic import BaseModel, ConfigDict


class TimeseriesPoint(BaseModel):
    time: str
    value: float


class ServiceStatus(BaseModel):
    name: str
    status: str          # "healthy" | "warning" | "critical"
    latency_ms: float
    rps: float
    error_rate: float    # percent, e.g. 0.02 means 0.02%
    simulated: bool       # False once real data has been ingested for this service


class SensorReading(BaseModel):
    value: float
    unit: str
    simulated: bool


class BuoyStatus(BaseModel):
    id: str
    status: str            # "healthy" | "warning" | "critical"
    status_text: str       # human label, e.g. "sensor fault"
    lat: float
    lng: float
    battery: float
    battery_simulated: bool
    solar_watts: float
    satellites: int
    signal_dbm: float
    position_simulated: bool
    gnss_fix: str
    last_contact_seconds: float
    sensors: dict[str, SensorReading]   # arbitrary sensor names - heterogeneous per buoy


class Alert(BaseModel):
    id: str
    time: str
    severity: str          # "critical" | "warning" | "info" | "resolved"
    source_type: str        # "service" | "buoy"
    source: str
    message: str


class Incident(BaseModel):
    id: str
    title: str
    severity: str
    status: str             # "ongoing" | "resolved"
    source_type: str
    source: str
    started_at: str
    resolved_at: Optional[str]
    cause: str
    events: list[Alert]


# ---------------------------------------------------------------- registration

class ServiceCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "name": "payments-api", "warn_ms": 300, "crit_ms": 600
    }})
    name: str
    warn_ms: float = 400.0
    crit_ms: float = 800.0


class BuoyCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "id": "buoy-07", "lat": 36.92, "lng": -76.21,
        "sensors": ["temp", "dissolved_o2"], "units": {"dissolved_o2": "mg/L"}
    }})
    id: str
    lat: float
    lng: float
    sensors: list[str] = []
    units: dict[str, str] = {}   # optional: {"dissolved_o2": "mg/L"} - unspecified sensors get no unit


class SensorCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"sensor": "dissolved_o2", "unit": "mg/L"}})
    sensor: str
    unit: str = ""


class SimulateRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"sensor": "turbidity"}})
    sensor: Optional[str] = None   # None = everything on this buoy (all sensors + battery + position)


# ---------------------------------------------------------------- ingest

class ServiceIngestReading(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "latency_ms": 142.0, "rps": 2100, "error_rate": 0.05
    }})
    latency_ms: Optional[float] = None
    rps: Optional[float] = None
    error_rate: Optional[float] = None
    time: Optional[str] = None


class BuoyIngestReading(BaseModel):
    """Extra fields aren't rejected — an unrecognized name is treated as a
    sensor reading and auto-registered on the buoy if it doesn't exist yet."""
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "temp": 18.4, "turbidity": 3.1, "battery": 82
    }})

    battery: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    satellites: Optional[int] = None
    signal_dbm: Optional[float] = None
    time: Optional[str] = None


class BuoyHeartbeat(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"signal_dbm": -71}})
    signal_dbm: Optional[float] = None
