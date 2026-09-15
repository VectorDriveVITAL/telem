from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class Model(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class TimeseriesPoint(Model):
    time: str
    value: float


class SensorRules(Model):
    enabled: bool = True
    warn_min: Optional[float] = None
    warn_max: Optional[float] = None
    crit_min: Optional[float] = None
    crit_max: Optional[float] = None
    stale_after_seconds: int = Field(300, ge=5, le=604800)

    @model_validator(mode="after")
    def ordered(self):
        for lo, hi in [("warn_min", "warn_max"), ("crit_min", "crit_max")]:
            a, b = getattr(self, lo), getattr(self, hi)
            if a is not None and b is not None and a >= b:
                raise ValueError(f"{lo} must be below {hi}")
        if (
            self.crit_min is not None
            and self.warn_min is not None
            and self.crit_min > self.warn_min
        ):
            raise ValueError("crit_min must be at or below warn_min")
        if (
            self.crit_max is not None
            and self.warn_max is not None
            and self.crit_max < self.warn_max
        ):
            raise ValueError("crit_max must be at or above warn_max")
        return self


class ServiceStatus(Model):
    name: str
    status: str
    latency_ms: float
    rps: float
    error_rate: float
    simulated: bool
    last_reading_at: Optional[str] = None
    stale: bool = False
    warn_ms: float = 400
    crit_ms: float = 800
    latency_delta_ms: float = 0


class SensorReading(Model):
    value: float
    unit: str
    simulated: bool
    last_reading_at: Optional[str] = None
    stale: bool = False
    status: str = "healthy"
    rules: SensorRules = Field(default_factory=SensorRules)


class BuoyStatus(Model):
    id: str
    status: str
    status_text: str
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
    sensors: dict[str, SensorReading]
    last_contact_at: Optional[str] = None
    contact_simulated: bool = True
    offline: bool = False
    offline_after_seconds: int = 300
    stale_sensor_count: int = 0
    field_metadata: dict = Field(default_factory=dict)
    mooring_lat: float = 0
    mooring_lng: float = 0


class Alert(Model):
    id: str
    time: str
    severity: str
    source_type: str
    source: str
    message: str
    acknowledged_at: Optional[str] = None
    muted: bool = False


class Incident(Model):
    id: str
    title: str
    severity: str
    status: str
    source_type: str
    source: str
    started_at: str
    resolved_at: Optional[str] = None
    cause: str
    events: list[Alert]
    owner: str = ""
    acknowledged_at: Optional[str] = None
    notes: list[dict] = Field(default_factory=list)


class ServiceCreate(Model):
    name: Identifier
    warn_ms: float = Field(400, gt=0)
    crit_ms: float = Field(800, gt=0)

    @model_validator(mode="after")
    def thresholds(self):
        if self.warn_ms >= self.crit_ms:
            raise ValueError("warn_ms must be below crit_ms")
        return self


class BuoyCreate(Model):
    id: Identifier
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    sensors: list[Identifier] = Field(default_factory=list, max_length=64)
    units: dict[str, str] = Field(default_factory=dict)


class SensorCreate(Model):
    sensor: Identifier
    unit: str = Field("", max_length=30)


class SimulateRequest(Model):
    sensor: Optional[str] = None


class TimedReading(Model):
    time: Optional[str] = None

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        if value is None:
            return value
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (dt - datetime.now(timezone.utc)).total_seconds() > 300:
            raise ValueError(
                "reading time cannot be more than five minutes in the future"
            )
        return dt.isoformat()


class ServiceIngestReading(TimedReading):
    latency_ms: Optional[float] = Field(None, ge=0)
    rps: Optional[float] = Field(None, ge=0)
    error_rate: Optional[float] = Field(None, ge=0, le=100)


class BuoyIngestReading(TimedReading):
    """Arbitrary finite numeric sensor fields remain supported."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    __pydantic_extra__: dict[str, Optional[float]] = Field(init=False)
    battery: Optional[float] = Field(None, ge=0, le=100)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    satellites: Optional[int] = Field(None, ge=0, le=100)
    signal_dbm: Optional[float] = None
    solar_watts: Optional[float] = Field(None, ge=0)

    @model_validator(mode="after")
    def sensor_names(self):
        import re

        if len(self.model_extra or {}) > 64:
            raise ValueError("at most 64 sensor fields per reading")
        for key in self.model_extra or {}:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", key):
                raise ValueError(
                    "sensor names must use letters, numbers, dots, underscores or hyphens"
                )
        return self


class BuoyHeartbeat(Model):
    signal_dbm: Optional[float] = None


class BuoySettings(Model):
    offline_after_seconds: int = Field(300, ge=5, le=604800)
    mooring_lat: Optional[float] = Field(None, ge=-90, le=90)
    mooring_lng: Optional[float] = Field(None, ge=-180, le=180)


class IncidentUpdate(Model):
    status: Optional[Literal["ongoing", "resolved"]] = None
    owner: Optional[str] = Field(None, max_length=120)
    acknowledged: Optional[bool] = None
    cause: Optional[str] = Field(None, max_length=2000)


class NoteCreate(Model):
    text: str = Field(min_length=1, max_length=2000)
    author: str = Field("Operator", min_length=1, max_length=120)

    @field_validator("text", "author")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("cannot be blank")
        return value.strip()


class MaintenanceCreate(Model):
    kind: Literal[
        "deployment",
        "calibration",
        "probe_swap",
        "battery_replacement",
        "site_visit",
        "other",
    ]
    description: str = Field(min_length=1, max_length=2000)
    operator: str = Field("Operator", min_length=1, max_length=120)
    sensor: Optional[Identifier] = None
    time: Optional[datetime] = None


class AlertUpdate(Model):
    acknowledged: bool = True


class MuteCreate(Model):
    source_type: Literal["service", "buoy"]
    source: Identifier
    duration_minutes: int = Field(60, ge=1, le=10080)
