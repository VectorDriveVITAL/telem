# telem — coastal buoy & service telemetry dashboard

A FastAPI dashboard for buoy telemetry and service health, with real or simulated
readings in InfluxDB and durable fleet operations in SQLite. The API serves the
frontend directly; no frontend build is required.

Version 0.2 adds a fleet overview, per-probe rules and freshness, persistent
incident actions, multi-metric analysis, maintenance logs and working CSV
exports. Existing routes and the legacy single-metric history response remain
available. Demo data is seeded only when the operational database is empty.

## Stack

- **Samples:** InfluxDB 2.7
- **Operational state:** SQLite (Python standard library)
- **API:** Python + FastAPI
- **Frontend:** HTML, CSS and vanilla JavaScript; bundled Leaflet 1.9.4

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
| `HISTORY_MINUTES` | `1440` | simulated history in minutes, seeded on first boot or upgraded once per field |
| `CORS_ORIGINS` | `*` | only matters if you serve the dashboard from somewhere else |
| `STATE_DB` | `.telem/state.sqlite3` | durable registrations, current state, rules, incidents, notes, maintenance and mutes |
| `SEED_DEMO` | `true` | seed demo sources on the first run; set `false` for an empty fleet |

## Project layout

```
app/
  main.py            FastAPI app, CORS, startup/shutdown, serves the dashboard at "/"
  config.py          env-var settings
  seed_data.py        the mock fleet's definition - edit this to add/change services & buoys
  generator.py        durable fleet controller, simulator, thresholds and operations
  state_store.py       SQLite snapshots, separate from InfluxDB samples
  influx_client.py     thin wrapper around the InfluxDB client (writes + Flux reads)
  models.py           Pydantic response models
  routers/
    services.py, buoys.py, alerts.py, incidents.py, ingest.py, operations.py
frontend/
  telemetry-dashboard.html   dashboard markup
  dashboard.js               interaction state and timestamp-based SVG charts
  operations.css             responsive operation views and dialogs
  vendor/leaflet/             bundled map library and license
tests/                       unit, real-database and browser workflows
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

The dashboard refreshes fleet state and the visible chart every 4 seconds after
the previous refresh completes. The demo fleet
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
| `POST /api/ingest/buoys/{id}` | any subset of `{"battery", "lat", "lng", "satellites", "signal_dbm", "solar_watts", "time"?}` **plus any sensor names you want**, e.g. `{"temp": 18.4, "turbidity": 3.1}` | **A field name that isn't a recognized sensor yet is auto-registered on that buoy** - you don't need to call `POST /api/buoys/{id}/sensors` first. It shows up with no unit unless you'd already registered it with one, and it appears as its own tab on the dashboard immediately. |
| `POST /api/ingest/buoys/{id}/batch` | array of the above | |
| `POST /api/ingest/buoys/{id}/heartbeat` | `{"signal_dbm"?}` | Updates last-contact only, doesn't touch sensor data. |

Numeric fields must be finite. Battery is constrained to 0–100, coordinates to
valid latitude/longitude ranges, and error rate to 0–100%. Invalid timestamps
are rejected; timezone-free timestamps are interpreted as UTC. Sensor values
remain unrestricted by alert limits: an out-of-range reading is recorded and
can trigger an incident. Null sensor fields are ignored for compatibility.
Batches accept 1–1,000 readings. Source and sensor names use letters, numbers,
dots, underscores and hyphens (maximum 80 characters).

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

Simulation resumes from the current reading and moves toward its configured
baseline. Real sensor fields are never rewritten by simulator ticks. Resuming
the entire buoy also resumes simulated contact; resuming one sensor does not.

## Using the new dashboard

- **Overview:** click an attention card to filter the relevant service, buoy or
  incident view. Rising latency means the latest change exceeds both 5 ms and
  5% of the current value. Offline and low-battery cards use the same rules as
  the source status shown elsewhere.
- **Sensor telemetry:** select a buoy to open its drawer. Add a probe, edit each
  probe's warning/critical limits, set its freshness deadline, or log maintenance.
  Settings controls the buoy's contact deadline and its individual mooring point.
- **Incidents:** expand a card to acknowledge it, assign an owner, edit the
  investigated cause, add notes, resolve it, or reopen it. Changes survive
  refresh and restart. Manual resolution does not change sensor health: the
  same unchanged condition stays resolved until a subsequent condition change.
  Only one incident per source can be ongoing at a time; older incidents remain.
- **Event feeds:** Ack is stored on the server. Mute is an expiring, shared
  source mute (default 60 minutes); incidents continue recording while muted.
- **Analysis:** select a buoy and any mix of probes, battery, solar, signal or
  position channels. Each metric is scaled to 0–100% by default; the legend and
  tooltip retain actual units and values. Shared numeric scale is also available.
  Gaps remain gaps; missing channels are not forward-filled.
- **Charts:** hover for recorded timestamps, use the wheel or +/- to zoom, drag
  to select a time window, or use the replay slider to move through recent
  history. Reset/Return to live restores the rolling window. Alert tracing
  opens the 30-minute window around the event. Dashed maintenance markers are
  shared with Analysis. Comparisons require matching metrics and units.
- **CSV:** choose raw samples or mean/min/max/last aggregation. Exports use the
  selected chart window, including zoom/replay; the dialog lets you adjust it.
  Incident export includes ownership, notes and event history.
- **Search:** Cmd+K or Ctrl+K finds all currently registered sources, including
  ones created after page load. Theme preference persists in the browser.

The Throughput and battery sparklines use recorded data. Error budget and HTTP
status-code cards explicitly show **Not reported** because the existing service
ingest contract does not provide the observations needed to calculate them.

## Operations API

| Endpoint | Purpose |
|---|---|
| `GET /api/overview` | Fleet counts, rising service names and prioritized sources |
| `GET/PUT /api/buoys/{id}/sensors/{sensor}/rules` | Per-probe value and freshness rules |
| `PUT /api/buoys/{id}/settings` | Contact deadline and mooring coordinates |
| `GET /api/incidents/{id}` | One complete incident |
| `PATCH /api/incidents/{id}` | `owner`, `cause`, `acknowledged`, `status` (`ongoing` or `resolved`) |
| `POST /api/incidents/{id}/notes` | `text` and optional `author` |
| `PATCH /api/alerts/{id}` | `{"acknowledged": true}` |
| `GET/POST /api/mutes` | List active mutes / mute a source for a duration |
| `DELETE /api/mutes/{id}` | Remove a source mute |
| `GET/POST /api/buoys/{id}/maintenance` | List or record work performed |
| `GET /api/export` | Raw or aggregated readings, or incident history, as CSV |

Example rule body (PUT replaces the complete rule):

```json
{
  "enabled": true,
  "warn_min": 5,
  "warn_max": null,
  "crit_min": 3,
  "crit_max": null,
  "stale_after_seconds": 300
}
```

Limits trigger when a value is strictly outside the configured range. Disabling
rules suppresses both value and freshness alerts for that probe, while the UI
continues showing whether its measurement is stale. Battery at or below 15% is
critical. Buoys become offline after their contact deadline (default 300 seconds).
Once any real ingest or heartbeat arrives, remaining simulated fields cannot
refresh that device's contact time. A heartbeat cannot refresh a probe's reading.

Maintenance accepts `kind` (`deployment`, `calibration`, `probe_swap`,
`battery_replacement`, `site_visit`, `other`), `description`, `operator`, optional
`sensor`, and optional ISO `time`. It is an activity log; a calibration entry
does not retroactively modify measurement values.

### History queries and exports

Existing single-metric calls still return `[{"time": "...", "value": 1.2}]`.
Specify `metrics` to receive a shared timeline with nulls for missing channels:

```text
GET /api/buoys/buoy-01/timeseries?metrics=temp,battery,solar_watts,signal_dbm&minutes=60
GET /api/services/telemetry-gateway/timeseries?metrics=latency_ms,rps,error_rate&minutes=60
```

```json
{
  "start": "2026-09-15T12:00:00+00:00",
  "end": "2026-09-15T13:00:00+00:00",
  "metrics": ["temp", "battery"],
  "aggregation": "mean",
  "interval_seconds": 15,
  "points": [{"time": "2026-09-15T12:00:00+00:00", "values": {"temp": 22, "battery": null}}]
}
```

History parameters: `start`, `end` (ISO timestamps), `minutes` (default 60),
`aggregation` (`raw`, `mean`, `min`, `max`, `last`), and `interval_seconds`.
Explicit timestamps take precedence over `minutes`. Multi-metric history
selects approximately 240 buckets by default; legacy calls retain approximately
60 buckets. Aggregated timestamps mark bucket starts, not arrival times.
Service latency is whatever latency statistic the producer submits; the API
averages that reported statistic per bucket rather than computing request p95.

Exports use these same time and aggregation parameters with `kind=buoy|service`,
`source=<id>` and optional comma-separated `metrics`. Use `kind=incidents` for
incidents started in the selected window. The end time is exclusive. Requests
are limited to 31 days and 20,000 returned metric values; overly large results
return HTTP 413 without silently truncating. Sensor rules and validation errors
return 400/422; unavailable telemetry storage returns 503.

Real readings include sensor metadata and measurement timestamps in current
state. Late batches are written to history, but an older real measurement does
not replace a newer real current value. InfluxDB writes use nanosecond timestamp
precision. New samples are tagged as real or simulated; pre-upgrade samples
remain readable without that tag.

## Persistence and running the app

Run **one Uvicorn worker**. The controller serializes mutations and snapshots
operational state into `STATE_DB`. Multiple worker processes would each own a
separate in-memory controller and must not share this state file. InfluxDB
continues to store the actual time series. SQLite is included with Python;
there is no additional database service to install.

The first upgrade starts operational state from the demo definitions unless
`SEED_DEMO=false`. Older releases kept registrations/incidents only in memory,
so there is no prior durable registry to migrate. Existing InfluxDB history is
retained. Subsequent restarts restore the fleet instead of reseeding it, even
when all sources have been deleted. Back up both InfluxDB and the state database
while the API is stopped. Runtime data, environments and credentials are ignored
by Git.

SQLite and InfluxDB do not share a distributed transaction. Invalid batches are
rejected before ingestion; if storage fails midway through a valid batch, some
historical writes may have succeeded. Retry using the same measurement timestamps.
Current operational state rolls back on a failed mutation. This remains a local
or trusted-network development dashboard with no user authentication.

Map tiles and optional web fonts need internet access. The Leaflet library is
bundled locally, and the rest of the dashboard can run without those services.

## Validation

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The ordinary suite uses temporary SQLite databases and substitutes the InfluxDB
writer. To exercise real Flux queries and CSV exports, start the development
app and run:

```bash
TELEM_TEST_URL=http://127.0.0.1:8000 python -m pytest tests/test_live.py -q
```

For browser workflows:

```bash
npm ci
npx playwright install chromium
TELEM_TEST_URL=http://127.0.0.1:8000 npm run test:browser
```

These integration/browser checks create uniquely named test sources, then
remove them. Their historical samples and incident records remain in the test
instance. Use a development instance. Browser screenshots and CSVs are written
to ignored `test-results/`. `CHROMIUM_EXECUTABLE` optionally selects an existing
Chromium binary.

## Extending it

`app/seed_data.py` defines only the first-run demo. Register sources and probes
through the API or dashboard, supply a display unit, and configure sensor rules.
The sidebar, map, command palette, chart selectors and Analysis discover them
from the API; no client-side sensor list needs editing.

### Chart responsiveness and motion

Chart updates morph compatible traces over 420 ms and crossfade when samples or
series change. Tab panels and throughput/battery readouts animate too. Motion
respects the operating system's reduced-motion setting. Wheel zoom previews
loaded readings immediately and batches a scroll gesture into one history request
after 140 ms of inactivity. Scale changes use the loaded data without a request.
Identical history requests share an in-flight response and a bounded four-second
cache; failed requests are never cached. New source selections issue one history
load, with maintenance and drawer loading in parallel. Influx field filters use
explicit equality predicates so storage filtering can be pushed down.

The browser test checks trace retention, immediate zoom preview, one request per
wheel burst, reduced motion, and the existing operational workflows. Its local
interaction timing is a smoke check, not a guarantee of database or network latency.

### Full-day demo history

The default demo seed covers the previous 24 hours at one-minute intervals for
all service metrics, every registered simulated probe, battery, solar output,
signal strength, satellite count and coordinates. Values vary over time, with
a daytime solar cycle and bounded sensor values. Short and full-day chart windows
therefore have recorded data immediately. The 7-day view shows the available day;
the seed does not invent a week's history.

Existing installations backfill simulated fields on their next restart without
resetting registrations, incidents, settings or latest readings. Real fields are
excluded. A durable per-field marker prevents repeated seeding at the same horizon.
`SEED_DEMO=false` disables backfill, and an explicit `HISTORY_MINUTES` overrides
the default; change an old `HISTORY_MINUTES=240` in your `.env` to `1440`.

### Dashboard regression coverage

The browser suite checks that Overview, Service health, Sensor telemetry,
Incidents and Analysis remain available. Sensor telemetry and Analysis both
expose the selected buoy's registered probes plus battery, solar, signal,
satellites and coordinates. Metric buttons wrap on smaller screens.
Sparse seed samples and dense live samples retain their traces during zoom;
isolated samples render as dots and absent values from other metrics do not
interrupt a valid trace. Regression checks also cover animations, comparison
controls, replay, exports, maintenance and incident workflows.
