#!/usr/bin/env bash
# macOS: Homebrew InfluxDB 2. Windows Git Bash: Docker Desktop InfluxDB 2.
# Run from Git Bash or Terminal: bash run.sh
# Existing InfluxDB data and .env are preserved. Ctrl+C stops only the API.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fail() { echo "ERROR: $*" >&2; exit 1; }
case "$(uname -s)" in
  Darwin*) PLATFORM=mac ;;
  MINGW*|MSYS*) PLATFORM=windows ;;
  *) fail "Supported platforms: macOS and Windows Git Bash." ;;
esac
command -v curl >/dev/null || fail "curl is required. On Windows, use Git Bash."
[ -f requirements.txt ] && [ -f app/main.py ] || fail "Place run.sh in the telem-backend folder."

echo "==> Setting up Python virtualenv..."
if [ "$PLATFORM" = windows ]; then
  VENV_PY=.venv/Scripts/python.exe
else
  VENV_PY=.venv/bin/python
fi
if [ ! -f "$VENV_PY" ]; then
  [ ! -e .venv ] || fail "The existing .venv is incomplete or from another OS. Move it aside, then rerun."
  PYTHON=()
  if [ "$PLATFORM" = windows ] && command -v py >/dev/null && py -3 -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    PYTHON=(py -3)
  else
    for candidate in python3 python; do
      if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
        PYTHON=("$candidate")
        break
      fi
    done
  fi
  [ "${#PYTHON[@]}" -gt 0 ] || fail "Install Python 3.10+ and reopen your terminal."
  "${PYTHON[@]}" -m venv .venv
fi
"$VENV_PY" -c 'import sys; assert sys.version_info >= (3, 10)' || fail "Recreate .venv with Python 3.10+."
"$VENV_PY" -m pip install -q -r requirements.txt

# Parse dotenv as data (never source .env as shell code).
# Environment overrides .env; defaults apply only to missing values.
CONFIG_EXPORTS=$("$VENV_PY" - <<'PY'
import os, shlex
from dotenv import dotenv_values
values = dotenv_values('.env')
defaults = dict(INFLUX_URL='http://localhost:8086', INFLUX_ORG='telem',
    INFLUX_BUCKET='telemetry', INFLUX_TOKEN='dev-super-secret-token',
    INFLUX_USER='admin', INFLUX_PASS='telemetry123', TICK_SECONDS='3',
    HISTORY_MINUTES='240', CORS_ORIGINS='*', PORT='8000')
for key, default in defaults.items():
    value = os.environ.get(key, values.get(key) or default)
    print('export ' + key + '=' + shlex.quote(value))
PY
)
eval "$CONFIG_EXPORTS"
unset CONFIG_EXPORTS
INFLUX_URL="${INFLUX_URL%/}"
export INFLUX_URL

healthy() {
  local response
  response=$(curl -fsS --connect-timeout 2 --max-time 3 "${INFLUX_URL}/health" 2>/dev/null) || return 1
  printf '%s' "$response" | "$VENV_PY" -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="pass" and str(d.get("version", "")).lstrip("v").startswith("2.") else 1)' 2>/dev/null
}

echo "==> Checking InfluxDB 2.x..."
if healthy; then
  echo "    Reusing the running InfluxDB 2.x instance."
else
  case "$INFLUX_URL" in
    http://localhost:8086|http://127.0.0.1:8086) ;;
    *) fail "Configured INFLUX_URL is unavailable or is not InfluxDB 2.x. Start that instance first." ;;
  esac
  if curl -s --max-time 2 -o /dev/null "${INFLUX_URL}/health"; then
    fail "Port 8086 is responding but is not healthy InfluxDB 2.x. Check the existing service; it has not been stopped."
  fi
  if [ "$PLATFORM" = mac ]; then
    command -v brew >/dev/null || fail "Install Homebrew first."
    if ! brew list --versions influxdb@2 >/dev/null 2>&1; then
      brew install influxdb@2
    fi
    INFLUXD_BIN="$(brew --prefix influxdb@2)/bin/influxd"
    mkdir -p .influxd-data
    nohup "$INFLUXD_BIN" \
      --bolt-path "$(pwd)/.influxd-data/influxd.bolt" \
      --engine-path "$(pwd)/.influxd-data/engine" \
      > .influxd.log 2>&1 &
    echo $! > .influxd.pid
    echo "    Started InfluxDB; log: .influxd.log"
  else
    command -v docker >/dev/null || fail "Install Docker Desktop, start it in Linux containers mode, then rerun."
    docker info >/dev/null 2>&1 || fail "Start Docker Desktop in Linux containers mode, then rerun."
    if docker container inspect telem-influxdb >/dev/null 2>&1; then
      docker start telem-influxdb >/dev/null
    else
      # Disable Git Bash path rewriting for Linux container volume paths.
      MSYS_NO_PATHCONV=1 docker run -d --name telem-influxdb \
        -p 127.0.0.1:8086:8086 \
        -v telem-influxdb-data:/var/lib/influxdb2 \
        -v telem-influxdb-config:/etc/influxdb2 \
        influxdb:2.7 >/dev/null
    fi
    echo "    Started Docker container telem-influxdb."
  fi
fi

echo "==> Waiting for InfluxDB..."
READY=false
for ((i=1; i<=60; i++)); do
  if healthy; then READY=true; break; fi
  sleep 1
done
if [ "$READY" != true ]; then
  if [ "$PLATFORM" = windows ]; then
    echo "Inspect logs: docker logs telem-influxdb" >&2
  else
    echo "Inspect logs: tail -30 .influxd.log" >&2
  fi
  fail "InfluxDB 2.x did not become healthy."
fi

echo "==> Checking first-time setup and credentials..."
# JSON encoding and HTTP error handling are done in Python so special characters
# in credentials work correctly. No token is printed or placed on curl's argv.
"$VENV_PY" - <<'PY'
import json, os, sys
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
base = os.environ['INFLUX_URL']
def request(path, payload=None, authenticated=False):
    headers = {'Content-Type': 'application/json'}
    if authenticated:
        headers['Authorization'] = 'Token ' + os.environ['INFLUX_TOKEN']
    req = Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    with urlopen(req, timeout=15) as response:
        return json.load(response)
try:
    if request('/api/v2/setup').get('allowed'):
        request('/api/v2/setup', dict(username=os.environ['INFLUX_USER'],
            password=os.environ['INFLUX_PASS'], org=os.environ['INFLUX_ORG'],
            bucket=os.environ['INFLUX_BUCKET'], token=os.environ['INFLUX_TOKEN'],
            retentionPeriodSeconds=0))
        print('    Initial setup complete.')
    result = request('/api/v2/buckets?' + urlencode({'org': os.environ['INFLUX_ORG'],
        'name': os.environ['INFLUX_BUCKET']}), authenticated=True)
    if not any(b.get('name') == os.environ['INFLUX_BUCKET'] for b in result.get('buckets', [])):
        sys.exit('ERROR: Configured bucket was not found. Check INFLUX_ORG and INFLUX_BUCKET in .env.')
    print('    Token and bucket verified.')
except HTTPError as exc:
    sys.exit(f'ERROR: InfluxDB returned HTTP {exc.code}. Check INFLUX_TOKEN, INFLUX_ORG, and INFLUX_BUCKET in .env (and any environment overrides), then rerun. Existing data was preserved.')
except (URLError, ValueError) as exc:
    sys.exit(f'ERROR: InfluxDB setup/check failed: {exc}')
PY

if [ ! -f .env ]; then
  "$VENV_PY" - <<'PY'
import os
from dotenv import set_key
for key in ('INFLUX_URL', 'INFLUX_TOKEN', 'INFLUX_ORG', 'INFLUX_BUCKET',
            'TICK_SECONDS', 'HISTORY_MINUTES', 'CORS_ORIGINS'):
    set_key('.env', key, os.environ[key])
PY
  echo "==> Created .env."
else
  echo "==> Keeping existing .env."
fi

# PORT may be supplied in .env or the environment; the environment wins.
# Try that port, the next 99 ports, then an OS-assigned port. This is a
# preflight check; Uvicorn still reports an error if another process takes
# the selected port during the short interval before server startup.
echo "==> Selecting API port (preferred: ${PORT})..."
API_PORT=$("$VENV_PY" - <<'PYPORT'
import errno, os, socket, sys
try:
    preferred = int(os.environ['PORT'])
    if not 1 <= preferred <= 65535:
        raise ValueError
except ValueError:
    sys.exit('ERROR: PORT must be an integer between 1 and 65535.')

def probe(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match exclusive Windows server ownership; do not reuse another
        # process's port. Also catch Windows excluded/reserved port ranges.
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(('0.0.0.0', port))
        sock.listen(1)
        return sock.getsockname()[1]

selected = None
for port in range(preferred, min(preferred + 100, 65536)):
    try:
        selected = probe(port)
        break
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES) and getattr(exc, 'winerror', None) not in (10013, 10048):
            sys.exit(f'ERROR: Cannot bind API socket: {exc}')
        if port == preferred:
            print(f'    Port {preferred} is busy or blocked; looking for another port.', file=sys.stderr)
if selected is None:
    try:
        selected = probe(0)
    except OSError as exc:
        sys.exit(f'ERROR: Windows/network policy prevented selecting an API port: {exc}')
print(selected)
PYPORT
)
export PORT="$API_PORT"
echo "==> Starting API and dashboard on port ${API_PORT}..."
echo "    Local dashboard: http://localhost:${API_PORT}/"
echo "    Local Swagger:   http://localhost:${API_PORT}/docs"
echo "    LAN Swagger:     http://<this-computer-LAN-IP>:${API_PORT}/docs"
if [ "$API_PORT" != 8000 ]; then
  echo "    Clients configured for port 8000 must use port ${API_PORT} for this run."
fi
if [ "$PLATFORM" = windows ]; then
  echo "    Find your LAN IPv4 address with: ipconfig"
  echo "    If Windows Firewall prompts, allow Python on your private network."
else
  echo "    Find your Wi-Fi IP with: ipconfig getifaddr en0"
fi
echo "    Wait for 'Application startup complete' before opening the page."
echo "    Ctrl+C stops the API; InfluxDB stays running."
exec "$VENV_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT" --reload
