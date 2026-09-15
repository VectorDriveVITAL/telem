#!/usr/bin/env bash
# Stops the influxd process started by run.sh (the one backgrounded with
# nohup, tracked via .influxd.pid) - not needed if you're using
# `brew services` or docker-compose instead.
set -euo pipefail

if [ ! -f .influxd.pid ]; then
  echo "No .influxd.pid found - nothing to stop (or it wasn't started by run.sh)."
  exit 0
fi

PID=$(cat .influxd.pid)
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped influxd (pid $PID)."
else
  echo "Process $PID isn't running - already stopped."
fi
rm -f .influxd.pid
