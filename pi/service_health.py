#!/usr/bin/env python3
"""Report this Pi's real services to telem's Service Health tab (stdlib only).

Every INTERVAL seconds, for each systemd unit below: `systemctl is-active`, plus the TCP connect time
where the unit has a port. latency_ms is that connect time; units without a port report 0 while active.
A unit that is not active or not accepting connections reports DOWN_MS, which telem shows as critical.
"""
import argparse
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

UNITS = {  # unit -> TCP port (None: no port to time)
    "mosquitto": 1883,
    "influxdb": 8086,
    "telem": 8000,
    "chirpstack-sqlite": 8080,
    "telegraf": None,
    "lora-pkt-fwd": None,
    "chirpstack-gateway-bridge": None,
    "lorawan-adapter": None,
}
DOWN_MS = 5000.0  # above the crit_ms registered below


def check(unit, port):
    active = subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
    if not active:
        return DOWN_MS, 100.0
    if port is None:
        return 0.0, 0.0
    start = time.monotonic()
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return DOWN_MS, 100.0
    return round((time.monotonic() - start) * 1000, 2), 0.0


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409: already registered
            raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--telem", default="http://127.0.0.1:8000")
    ap.add_argument("--interval", type=float, default=15)  # telem marks a service stale after 300 s
    args = ap.parse_args()
    while True:
        for unit, port in UNITS.items():
            latency, err = check(unit, port)
            try:
                post(f"{args.telem}/api/services", {"name": unit, "warn_ms": 500, "crit_ms": 1000})
                post(f"{args.telem}/api/ingest/services/{unit}", {"latency_ms": latency, "error_rate": err})
            except (urllib.error.URLError, OSError) as e:
                print(f"{unit}: telem unreachable: {e}", flush=True)
                break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
