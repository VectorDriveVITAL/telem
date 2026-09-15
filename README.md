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

## Running without Docker

If you'd rather not install Docker, InfluxDB runs as a plain native binary
too — the `docker compose up -d` step is the only thing Docker was doing
for you here, everything else is identical either way.

**On macOS, `./run.sh` automates everything below** (install `influxdb@2`
if needed, start it, run first-time setup, write `.env`, set up the
venv, and launch the API) and is safe to re-run. It deliberately starts
`influxd` directly in the background with `nohup` rather than through
`brew services` — `brew services` goes through macOS's `launchd`, which
can fail with an opaque "Bootstrap failed" error for reasons unrelated to
InfluxDB itself (stale launch agents, permission issues), and sidestepping
it entirely turned out to be more reliable than debugging launchd. Run
`./stop.sh` to stop the InfluxDB process `run.sh` started. The manual
steps below are what `run.sh` is doing, if you want to run them by hand or
adapt them for Windows/Linux.

### macOS

**1. Install InfluxDB with Homebrew**

The plain `influxdb` formula now installs **InfluxDB 3.x** — a different
product (different CLI, different data model, no orgs/buckets/tokens the
way this project expects). You want the version-pinned 2.x formula
instead:

```bash
brew update
brew install influxdb@2
```

**2. Start it and leave it running**

`influxdb@2` is keg-only, so Homebrew won't put it on your `PATH`
automatically — start it one of these two ways:

```bash
# Option A: run in a terminal window you keep open
$(brew --prefix influxdb@2)/bin/influxd

# Option B: run as a persistent background service instead
# (goes through macOS launchd - if this fails with "Bootstrap failed",
# use Option A instead rather than debugging launchd)
brew services start influxdb@2
```

Either way it listens on port 8086 by default. (If you already ran `brew
install influxdb` and hit `zsh: command not found: influxd`, that's the 3.x
mismatch above — clean it up first with `brew services stop influxdb &&
brew uninstall influxdb`, then use `influxdb@2` instead.)

**3. First-time setup, via the browser**

The `influx` CLI is a separate formula (`brew install influxdb-cli`) if you
want it, but the setup wizard in the browser is simplest and needs nothing
extra installed:

1. Open **http://localhost:8086**
2. Click **Get Started**
3. Fill in:
   - Username: `admin`, Password: anything you like
   - **Organization Name: `telem`**
   - **Bucket Name: `telemetry`**
4. On the confirmation screen it shows you an API token — click to
   reveal/copy it. (If you miss it, it's always at **Load Data → API
   Tokens** in the left sidebar afterward.)

**4. Point the project at your real token**

Copy `.env.example` to `.env` and paste in the token you just copied:

```
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=<paste the token you copied>
INFLUX_ORG=telem
INFLUX_BUCKET=telemetry
```

Org and bucket already match what you typed in step 3, so only the token
line actually needs changing.

**5. Run the Python side — a second terminal tab**

```bash
cd path/to/telem-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**6. Open it**

**http://localhost:8000/**

You end up with two terminal windows running the whole time (or one
terminal plus the background service from Option B): one running
`influxd`, one running `uvicorn`. Close either and that half of the stack
stops.

### Windows

**1. Install InfluxDB itself**

Open **PowerShell**:

```powershell
# Download InfluxDB for Windows
cd $HOME\Downloads
Invoke-WebRequest -Uri "https://download.influxdata.com/influxdb/releases/influxdb2-2.9.1-windows_amd64.zip" -OutFile "influxdb2.zip"

# Extract it somewhere simple (avoiding Program Files skips needing admin rights)
Expand-Archive .\influxdb2.zip -DestinationPath "$HOME\influxdb"
Rename-Item "$HOME\influxdb\influxdb2-2.9.1-windows_amd64" "$HOME\influxdb\server"
```

**2. Start it and leave it running**

```powershell
cd "$HOME\influxdb\server"
.\influxd.exe
```

Keep this PowerShell window open — this **is** your database now. The first
time you run it, Windows Defender Firewall will pop up asking about network
access: check **Private networks** and click **Allow access**.

**3. First-time setup, via the browser**

The `influx` CLI is a separate download, and scripting the setup call from
PowerShell means fighting JSON-quoting rules — the browser UI is simplest:

1. Open **http://localhost:8086**
2. Click **Get Started**
3. Fill in:
   - Username: `admin`, Password: anything you like
   - **Organization Name: `telem`**
   - **Bucket Name: `telemetry`**
4. On the confirmation screen it shows you an API token — click to
   reveal/copy it. (If you miss it, it's always at **Load Data → API
   Tokens** in the left sidebar afterward.)

**4. Point the project at your real token**

Copy `.env.example` to `.env` and paste in the token you just copied:

```
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=<paste the token you copied>
INFLUX_ORG=telem
INFLUX_BUCKET=telemetry
```

Org and bucket already match what you typed in step 3, so only the token
line actually needs changing.

**5. Run the Python side — a second PowerShell window**

```powershell
cd path\to\telem-backend
py -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell refuses that last line ("running scripts is disabled"), that's
execution policy — fix it with `Set-ExecutionPolicy -Scope Process
-ExecutionPolicy Bypass` and re-run the `Activate.ps1` line.

```powershell
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**6. Open it**

**http://localhost:8000/**

You end up with two PowerShell windows running the whole time: one running
`influxd.exe`, one running `uvicorn`. Close either and that half of the
stack stops.

One version note: `docker-compose.yml` is pinned to InfluxDB **2.7**, but
2.9.1 is what's currently linked for native Windows — that's fine, it's the
same v2 API the whole project talks to, nothing else to adjust for the
version bump.

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

### Reading data

| Endpoint | Returns |
|---|---|
| `GET /api/services` | current status for every registered service |
| `GET /api/services/{name}/timeseries?minutes=60` | p95 latency history |
| `GET /api/buoys` | current status for every registered buoy, including its actual sensor set |
| `GET /api/buoys/{id}/timeseries?metric=<sensor>&minutes=60` | one sensor channel's history - `<sensor>` is whatever that buoy actually has, not a fixed list |
| `GET /api/alerts?limit=20` | recent alert feed |
| `GET /api/incidents` | alerts grouped into incidents, with open/resolved state |
| `GET /api/health` | uptime check |

The dashboard polls the first six of these every 4 seconds. The demo fleet
(6 services, 6 buoys with `temp`/`salinity`/`turbidity`/`ph`) is just what's
registered on first boot - none of it is hardcoded beyond that.

### Registering services and buoys

Nothing is a fixed list - register what you want at runtime.

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/services` | `{"name", "warn_ms"?, "crit_ms"?}` | 409 if the name exists. Starts out simulated. |
| `DELETE /api/services/{name}` | - | Historical InfluxDB data is left alone. |
| `POST /api/buoys` | `{"id", "lat", "lng", "sensors"?: [...], "units"?: {...}}` | `sensors` can be `[]` - a buoy with only battery/GNSS is valid. `units` maps sensor name → display unit, e.g. `{"dissolved_o2": "mg/L"}`. |
| `DELETE /api/buoys/{id}` | - | |
| `POST /api/buoys/{id}/sensors` | `{"sensor", "unit"?}` | Add a sensor to an existing buoy later. 409 if it already exists. |
| `DELETE /api/buoys/{id}/sensors/{sensor}` | - | |

### Ingesting real data

Each field you send flips *only that field* from simulated to real - a
service's latency/rps/error_rate move together, but a buoy's sensors,
battery, and position are tracked independently. A buoy can have some
sensors on real hardware and others still simulated at the same time.

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/ingest/services/{name}` | any subset of `{"latency_ms", "rps", "error_rate", "time"?}` | 404 if not registered. `time` optional, defaults to now. |
| `POST /api/ingest/services/{name}/batch` | array of the above | For backfill or batched uplink. |
| `POST /api/ingest/buoys/{id}` | any subset of `{"battery", "lat", "lng", "satellites", "signal_dbm", "time"?}` **plus any sensor names you want**, e.g. `{"temp": 18.4, "turbidity": 3.1}` | **A field name that isn't a recognized sensor yet is auto-registered on that buoy** - you don't need to call `POST /api/buoys/{id}/sensors` first. It shows up with no unit unless you'd already registered it with one, and it appears as its own tab on the dashboard immediately. |
| `POST /api/ingest/buoys/{id}/batch` | array of the above | |
| `POST /api/ingest/buoys/{id}/heartbeat` | `{"signal_dbm"?}` | Updates last-contact only, doesn't touch sensor data. |

There's no validation on ingested values (no range checking, no auth) -
this is a local dev tool, not a public-facing ingest pipeline. Add both
before this ever faces a network you don't trust.

**"Stopping simulation" isn't a separate action** - sending real data for a
field is what stops it being simulated, immediately, the moment the first
value arrives. There's no ingest panel in the dashboard UI (by design -
this is meant for a real device or script to call, not manual entry), and
a single POST just freezes that field at whatever value you sent until the
next one arrives - there's no interpolation. For anything you want to look
genuinely live, whatever's feeding it needs to keep POSTing on an interval.
`scripts/feed_sensor_example.py` is a minimal runnable template for that
loop - run it as-is against the demo fleet to see the mechanism work, or
replace its `read_sensors()` function with a real sensor read (serial,
I2C, a vendor SDK, whatever) and point `--api` at wherever this ends up
running:

```bash
pip install requests   # only needed for this example script, not the API itself
python3 scripts/feed_sensor_example.py --buoy buoy-01 --interval 5
```

### Resuming simulation

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/services/{name}/simulate` | - | Flips a service back to simulated, picking up its random walk from the current value. |
| `POST /api/buoys/{id}/simulate` | `{"sensor"?}` | With `sensor` given, flips just that one field back (`"battery"`, `"position"`, or a sensor name). Omitted → resets the whole buoy to simulated. |

One rough edge worth knowing: flipping a sensor back to simulated resumes
the random walk from whatever internal baseline it was assigned (0 for an
auto-registered sensor with no prior config) rather than from the last
real value - so you may see a jump on the chart right after resuming. Given
there's no validation layer, smoothing that over wasn't worth the
complexity yet.

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
  be literal geometry, that's what the real map is for. Newly-registered
  buoys don't get a position on this specific plot (they still show up
  everywhere else — sidebar, table, real map, drawer).
- No auth, no validation on ingested values (no range checking, no type
  strictness beyond "is it a number"). Fine for local dev; add both before
  this ever faces a network you don't trust.
- Buoy status (healthy/warning/critical) only reacts to battery level and
  the original four known metrics (turbidity/pH fault thresholds). A
  custom sensor added via registration or ingest is plotted and shown
  everywhere, but never drives an alert or incident on its own — there's
  no notion of a "normal range" for an arbitrary field name without
  validation.
- Resuming simulation on a sensor after real data stops (`POST
  /api/buoys/{id}/simulate`) picks up from an internal baseline, not the
  last real value — expect a visible jump on the chart right after.
- `docker-compose.yml` only runs InfluxDB, not the API — that's
  intentional for now, since `uvicorn --reload` during development is
  more convenient than rebuilding a container on every change. Add a
  second service to the compose file if you'd rather run everything in
  containers.

## Extending it

The fleet isn't a fixed list anymore — `app/seed_data.py` only defines
what's registered on first boot. Add a service or buoy at runtime with
`POST /api/services` / `POST /api/buoys` (see the API section above), or
just start sending `POST /api/ingest/buoys/{id}` with a field name that
doesn't exist yet and it registers itself. Both paths show up on the
dashboard immediately — new sidebar entry, new table row, and for buoys,
a metric tab per sensor, built from whatever that buoy actually reports
rather than a hardcoded list.

If you want a brand-new sensor to render nicely instead of falling back to
an auto-scaled y-axis, add it to `KNOWN_METRIC_DISPLAY` in the dashboard's
`<script>` with a fixed `min`/`max`/`decimals` — that's the one place a
"nice known range" is hardcoded client-side; everything else about a
custom sensor works without it.

