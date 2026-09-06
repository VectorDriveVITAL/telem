# telem — coastal buoy & service telemetry dashboard

A real backend for the telem dashboard: InfluxDB for storage, a FastAPI
service that runs a live mock-data simulator and serves it over a small
REST API, and the dashboard itself served straight off the API so there's
nothing to configure — one process, one URL.

The simulator isn't a scripted replay. Each service and buoy metric does a
bounded random walk in InfluxDB, and alerts/incidents are derived
server-side from real threshold crossings on that data. Restart it and
you'll get a different (but similarly-shaped) story each time.

## Stack

- **Storage:** InfluxDB 2.7
- **API:** Python + FastAPI (see the "why Python" note below)
- **Frontend:** the existing single-file dashboard, now fetching from the
  API instead of generating mock data client-side

## Quick start

```bash
# 1. Start InfluxDB (auto-initializes org/bucket/token on first run)
docker compose up -d

# 2. Install Python deps
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt

# 3. Run the API (also serves the dashboard)
uvicorn app.main:app --reload

# 4. Open the dashboard
open http://localhost:8000/
```

That's it — the dashboard, the API, and InfluxDB are all talking to each
other at that point. The first request takes a few seconds while it
backfills ~4 hours of synthetic history into InfluxDB so the charts aren't
empty on first load.

InfluxDB's own UI (if you want to poke at the raw data or write Flux
queries by hand) is at **http://localhost:8086** — login `admin` /
`telemetry123`.

## Configuration

Copy `.env.example` to `.env` if you want to change anything (a different
InfluxDB instance, tick rate, history depth). The defaults match
`docker-compose.yml` exactly, so you don't need a `.env` file at all for
the standard local setup.

| Variable | Default | What it does |
|---|---|---|
| `INFLUX_URL` | `http://localhost:8086` | |
| `INFLUX_TOKEN` | `dev-super-secret-token` | |
| `INFLUX_ORG` | `telem` | |
| `INFLUX_BUCKET` | `telemetry` | |
| `TICK_SECONDS` | `3` | how often the simulator writes a new point per series |
| `HISTORY_MINUTES` | `240` | how much synthetic history to backfill on first boot |
| `CORS_ORIGINS` | `*` | only matters if you serve the dashboard from somewhere else |

## Project layout

```
app/
  main.py            FastAPI app, CORS, startup/shutdown, serves the dashboard at "/"
  config.py          env-var settings
  seed_data.py        the mock fleet's definition - edit this to add/change services & buoys
  generator.py        the simulator: random walks, thresholds, alerts, incidents
  influx_client.py     thin wrapper around the InfluxDB client (writes + Flux reads)
  models.py           Pydantic response models
  routers/
    services.py, buoys.py, alerts.py, incidents.py
frontend/
  telemetry-dashboard.html   the dashboard - fetches from the API below
docker-compose.yml    InfluxDB, auto-initialized
requirements.txt
.env.example
```

## API

Interactive docs (courtesy of FastAPI) are at **http://localhost:8000/docs**
once the server's running. Summary:

| Endpoint | Returns |
|---|---|
| `GET /api/services` | current status for all 6 services |
| `GET /api/services/{name}/timeseries?minutes=60` | p95 latency history |
| `GET /api/buoys` | current status for all 6 buoys |
| `GET /api/buoys/{id}/timeseries?metric=temp&minutes=60` | one sensor channel's history (`temp`, `salinity`, `turbidity`, `ph`) |
| `GET /api/alerts?limit=20` | recent alert feed |
| `GET /api/incidents` | alerts grouped into incidents, with open/resolved state |
| `GET /api/health` | uptime check |

The dashboard polls the first six of these every 4 seconds.

## Why Python/FastAPI over TypeScript

InfluxDB's Python client is more mature than the JS one, FastAPI gives you
the `/docs` page above for free (genuinely useful while you're deciding
what a mock endpoint should return), and Pydantic models map cleanly onto
"a buoy reading" without much ceremony. The frontend is still a single
vanilla-JS file rather than a TS build, so TypeScript's main advantage —
sharing types between frontend and backend — doesn't apply here. If you
rebuild the frontend in TS at some point, that calculus changes.

## Known gaps

Being upfront about what's still cosmetic rather than wired to real data:

- The three small stat cards on the Service health tab — **Throughput**,
  **Error budget**, and **Status codes (5m)** — are still the original
  static mock numbers. The backend doesn't currently track a request-rate
  history, an SLO/error-budget calculation, or an HTTP-status-code
  breakdown, so there was nothing real to wire them to yet. Everything
  else on both tabs (sidebar, table, chart, alerts, incidents, drawer,
  map, compare mode) is live.
- The "Mooring drift" panel (the abstract schematic plot, not the real
  map) keeps its original stylized layout positions and only updates each
  buoy's status color live — the positions themselves were never meant to
  be literal geometry, that's what the real map is for.
- There's no auth on the API. Fine for local dev; add something before
  this ever sees a network you don't trust.
- `docker-compose.yml` only runs InfluxDB, not the API — that's
  intentional for now, since `uvicorn --reload` during development is
  more convenient than rebuilding a container on every change. Add a
  second service to the compose file if you'd rather run everything in
  containers.

## Extending it

Adding a service or buoy to the simulated fleet is just adding an entry to
`app/seed_data.py` — the generator, alerting, and API all pick it up
automatically. Adding a new metric (say, dissolved oxygen) means adding it
to a buoy's `base`/`volatility` dicts in `seed_data.py`, to
`METRIC_RANGES`, and to the `METRICS`/`METRIC_API_KEY` objects in the
dashboard's `<script>` — those three spots are the only places metric
names are hardcoded.
