#!/usr/bin/env bash
# Sets up InfluxDB 2.x (via Homebrew) and runs the telem API against it.
# Safe to re-run - each step checks whether it's already done.
set -euo pipefail

INFLUX_URL="http://localhost:8086"
INFLUX_ORG="telem"
INFLUX_BUCKET="telemetry"
INFLUX_TOKEN="dev-super-secret-token"
INFLUX_USER="admin"
INFLUX_PASS="telemetry123"

echo "==> Checking for the wrong (v3) influxdb formula..."
if brew list influxdb &>/dev/null; then
  echo "    Found plain 'influxdb' (this is InfluxDB 3.x, not what this project uses) - removing it."
  brew services stop influxdb &>/dev/null || true
  brew uninstall influxdb
fi

echo "==> Ensuring influxdb@2 is installed..."
if ! brew list influxdb@2 &>/dev/null; then
  brew install influxdb@2
fi

echo "==> Checking whether InfluxDB is already running..."
if curl -s --max-time 2 "${INFLUX_URL}/health" | grep -qE '"status":\s*"pass"'; then
  echo "    Already running - leaving it alone, not starting another copy."
else
  echo "==> Starting InfluxDB 2.x in the background..."
  # Deliberately NOT using `brew services` here - that goes through macOS
  # launchd, which can fail with cryptic "Bootstrap failed" errors for
  # reasons unrelated to InfluxDB itself (stale launch agents, permission
  # issues, etc). Running the binary directly and backgrounding it with
  # nohup sidesteps launchd entirely - simpler and more portable.
  INFLUXD_BIN="$(brew --prefix influxdb@2)/bin/influxd"
  mkdir -p .influxd-data
  nohup "$INFLUXD_BIN" \
    --bolt-path "$(pwd)/.influxd-data/influxd.bolt" \
    --engine-path "$(pwd)/.influxd-data/engine" \
    > .influxd.log 2>&1 &
  echo $! > .influxd.pid
  echo "    Started influxd (pid $(cat .influxd.pid)), logging to .influxd.log"
fi

echo "==> Waiting for InfluxDB to come up on ${INFLUX_URL}..."
for i in $(seq 1 30); do
  if curl -s --max-time 2 "${INFLUX_URL}/health" | grep -qE '"status":\s*"pass"'; then
    echo "    InfluxDB is up."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "    InfluxDB never came up. Things to check:" >&2
    echo "      - Is something else already using port 8086? Run: lsof -i :8086" >&2
    echo "      - What does the log say? Run: tail -30 .influxd.log" >&2
    exit 1
  fi
  sleep 1
done

echo "==> Running first-time setup (org/bucket/token) if not already done..."
SETUP_ALLOWED=$(curl -s "${INFLUX_URL}/api/v2/setup" | grep -o '"allowed": *true' || true)
if [ -n "$SETUP_ALLOWED" ]; then
  curl -s -X POST "${INFLUX_URL}/api/v2/setup" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${INFLUX_USER}\",\"password\":\"${INFLUX_PASS}\",\"org\":\"${INFLUX_ORG}\",\"bucket\":\"${INFLUX_BUCKET}\",\"token\":\"${INFLUX_TOKEN}\",\"retentionPeriodSeconds\":0}" \
    > /dev/null
  echo "    Set up org '${INFLUX_ORG}', bucket '${INFLUX_BUCKET}'."
else
  # Already onboarded - but by whom? If this is a leftover instance from an
  # earlier attempt (e.g. the browser setup wizard, which generates a random
  # real token, not this project's fixed one), our token won't work against
  # it and every write will 401 deep inside uvicorn's startup instead of
  # failing here with a clear reason. Check now, before that happens.
  echo "    Already set up - verifying this project's token still works against it..."
  TOKEN_OK=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Token ${INFLUX_TOKEN}" "${INFLUX_URL}/api/v2/buckets?org=${INFLUX_ORG}")
  if [ "$TOKEN_OK" != "200" ]; then
    echo "" >&2
    echo "    ERROR: InfluxDB on port 8086 is already set up, but not with this" >&2
    echo "    project's token (dev-super-secret-token) - got HTTP ${TOKEN_OK}." >&2
    echo "" >&2
    echo "    This usually means an instance from an earlier attempt is still" >&2
    echo "    running - e.g. one set up manually through the browser (which" >&2
    echo "    generates its own random token), or a leftover 'brew services'" >&2
    echo "    process this script no longer uses." >&2
    echo "" >&2
    echo "    Find and stop it, then re-run this script:" >&2
    echo "      lsof -i :8086                     # shows the PID using the port" >&2
    echo "      kill <PID>                        # stop it" >&2
    echo "      brew services stop influxdb@2     # in case it's the brew-managed one" >&2
    echo "" >&2
    echo "    Or, to keep that instance instead: after this script finishes," >&2
    echo "    edit INFLUX_TOKEN in .env to match its real token (find it at" >&2
    echo "    http://localhost:8086 -> Load Data -> API Tokens)." >&2
    exit 1
  fi
  echo "    Token OK - reusing the existing setup."
fi

echo "==> Writing .env (only if it doesn't already exist)..."
if [ ! -f .env ]; then
  cat > .env << EOF
INFLUX_URL=${INFLUX_URL}
INFLUX_TOKEN=${INFLUX_TOKEN}
INFLUX_ORG=${INFLUX_ORG}
INFLUX_BUCKET=${INFLUX_BUCKET}
TICK_SECONDS=3
HISTORY_MINUTES=240
CORS_ORIGINS=*
EOF
  echo "    .env created."
else
  echo "    .env already exists - leaving it alone."
fi

echo "==> Setting up the Python virtualenv..."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Starting the API - this also serves the dashboard."
echo "    Open http://localhost:8000/ once you see 'Application startup complete'."
echo ""
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
