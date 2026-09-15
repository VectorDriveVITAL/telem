import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from influxdb_client.rest import ApiException
from urllib3.exceptions import HTTPError

from .config import settings
from .generator import background_loop, seconds_since_start, seed_history
from .influx_client import QueryTooLarge, close_client
from .routers import alerts, buoys, incidents, ingest, operations, services

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_bg_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_task
    print("[startup] restoring fleet (demo history is seeded only on first run)...")
    await asyncio.get_event_loop().run_in_executor(None, seed_history)
    print("[startup] fleet ready")
    _bg_task = asyncio.create_task(background_loop())
    yield
    if _bg_task:
        _bg_task.cancel()
        try:
            await _bg_task
        except asyncio.CancelledError:
            pass
    # Wait for any executor tick before closing its InfluxDB connection.
    from . import generator

    await asyncio.to_thread(generator.flush_state)
    close_client()


app = FastAPI(
    title="telem API",
    description=(
        "Backend for the telem coastal buoy / service telemetry dashboard. "
        "Serves live (simulated + real) fleet data out of InfluxDB, and "
        "accepts real readings via the ingest endpoints below - anything "
        "you POST there takes over from the simulator for that field.\n\n"
        "The dashboard itself is served at [`/`](/) - this page is just for "
        "poking at the API directly."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(services.router)
app.include_router(buoys.router)
app.include_router(alerts.router)
app.include_router(incidents.router)
app.include_router(ingest.router)
app.include_router(operations.router)


@app.exception_handler(KeyError)
async def missing(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse(
        status_code=413 if isinstance(exc, QueryTooLarge) else 400,
        content={"detail": str(exc)},
    )


@app.exception_handler(ApiException)
@app.exception_handler(HTTPError)
async def storage_unavailable(request, exc):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Telemetry storage is unavailable. Check InfluxDB and retry."
        },
    )


@app.get("/api/health", summary="Health check", tags=["meta"])
def health():
    """Basic liveness/uptime check - doesn't touch InfluxDB."""
    return {"status": "ok", "uptime_seconds": round(seconds_since_start(), 1)}


# Serve the dashboard itself at "/", same-origin with the API so there's no
# CORS to think about for local dev — run this one process and open one URL.
if FRONTEND_DIR.exists():

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "telemetry-dashboard.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
