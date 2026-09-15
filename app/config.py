"""
Configuration for the telem backend.

Everything is read from environment variables so the same code works whether
you're running InfluxDB via docker-compose (the default setup this project
ships with) or against an existing InfluxDB instance you already have.
"""
import os

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

    # How much synthetic history to backfill on first startup, at 1-minute
    # resolution, so charts aren't empty the moment you open the dashboard.
    history_minutes: int = int(os.getenv("HISTORY_MINUTES", "240"))

    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")
    state_db: str = os.getenv("STATE_DB", ".telem/state.sqlite3")
    seed_demo: bool = os.getenv("SEED_DEMO", "true").lower() == "true"


settings = Settings()
