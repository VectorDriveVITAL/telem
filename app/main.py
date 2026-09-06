import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .generator import seed_history, background_loop, seconds_since_start
from .influx_client import close_client
from .routers import services, buoys, alerts, incidents

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_bg_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_task
    print("[startup] seeding history into InfluxDB (this takes a few seconds)...")
    await asyncio.get_event_loop().run_in_executor(None, seed_history)
    print("[startup] history seeded, starting live simulator")
    _bg_task = asyncio.create_task(background_loop())
    yield
    if _bg_task:
        _bg_task.cancel()
    close_client()


app = FastAPI(title="telem API", lifespan=lifespan)

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


@app.get("/api/health")
def health():
    return {"status": "ok", "uptime_seconds": round(seconds_since_start(), 1)}


# Serve the dashboard itself at "/", same-origin with the API so there's no
# CORS to think about for local dev — run this one process and open one URL.
if FRONTEND_DIR.exists():
    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "telemetry-dashboard.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
