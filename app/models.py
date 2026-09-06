from pydantic import BaseModel


class TimeseriesPoint(BaseModel):
    time: str
    value: float


class ServiceStatus(BaseModel):
    name: str
    status: str          # "healthy" | "warning" | "critical"
    latency_ms: float
    rps: float
    error_rate: float    # percent, e.g. 0.02 means 0.02%


class BuoyStatus(BaseModel):
    id: str
    status: str           # "healthy" | "warning" | "critical"
    status_text: str      # human label, e.g. "sensor fault"
    lat: float
    lng: float
    battery: float
    solar_watts: float
    temp: float
    salinity: float
    turbidity: float
    ph: float
    gnss_fix: str
    signal_dbm: int
    last_contact_seconds: float


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
    resolved_at: str | None
    cause: str
    events: list[Alert]
