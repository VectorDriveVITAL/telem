"""
Configuration for the telem backend.

Everything is read from environment variables so the same code works whether
you're running InfluxDB via docker-compose (the default setup this project
ships with) or against an existing InfluxDB instance you already have.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional - .env just won't auto-load without it


class Settings:
    influx_url: str = os.getenv("INFLUX_URL", "http://localhost:8086")
    influx_token: str = os.getenv("INFLUX_TOKEN", "dev-super-secret-token")
    influx_org: str = os.getenv("INFLUX_ORG", "telem")
    influx_bucket: str = os.getenv("INFLUX_BUCKET", "telemetry")

    # How often the background simulator writes a new point per series.
    tick_seconds: float = float(os.getenv("TICK_SECONDS", "3"))

    # How much synthetic history to backfill on first startup or upgrade, at 1-minute
    # resolution, so charts aren't empty the moment you open the dashboard.
    history_minutes: int = int(os.getenv("HISTORY_MINUTES", "1440"))

    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")
    state_db: str = os.getenv("STATE_DB", ".telem/state.sqlite3")
    seed_demo: bool = os.getenv("SEED_DEMO", "true").lower() == "true"

    # Forward real buoy readings to the VITAL sensor API; off unless all three are set.
    vital_api_url: str = os.getenv(
        "VITAL_API_URL", "https://vitalapi.pods.portals.tapis.io/api/v1"
    )
    vital_username: str = os.getenv("VITAL_USERNAME", "")
    vital_password: str = os.getenv("VITAL_PASSWORD", "")
    vital_campaign_id: str = os.getenv("VITAL_CAMPAIGN_ID", "")

    # Which buoy and service the bridge's readings and link health belong to.
    receiver_buoy: str = os.getenv("RECEIVER_BUOY", "buoy-01")
    receiver_service: str = os.getenv("RECEIVER_SERVICE", "lora-receiver")
    # When set, the bridge, flash and calibration controls need `Authorization: Bearer <token>`;
    # when empty they only accept same-origin requests from this computer.
    receiver_token: str = os.getenv("RECEIVER_TOKEN", "")

    # Sensors repo holding tools/bridge.py and flash.sh, for the Receiver tab.
    receiver_dir: str = os.getenv(
        "RECEIVER_DIR", str(Path(__file__).resolve().parents[2] / "TDS-and-Humidity-Temp-Sensors")
    )


settings = Settings()
