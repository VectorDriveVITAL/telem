#!/usr/bin/env python3
"""
Template for feeding real sensor data into telem continuously.

This is what "stopping simulation" actually looks like in practice: there's
no separate on/off switch for it (see README) - a field stays simulated
until the first real reading arrives for it, then holds at whatever was
last POSTed until the next one shows up. A real buoy needs to keep POSTing
on some interval to look "live" rather than frozen.

Run as-is and it demos against buoy-01 with fake-but-plausible numbers, so
you can see the mechanism work before wiring in real hardware. To point it
at real hardware: replace read_sensors() with whatever actually reads your
sensor (serial port, I2C, GPIO, a vendor SDK, etc.) and leave everything
else - the ingest call and the loop - as is.

Usage:
    python3 scripts/feed_sensor_example.py
    python3 scripts/feed_sensor_example.py --buoy buoy-02 --interval 10
    python3 scripts/feed_sensor_example.py --api http://192.168.1.50:8000
"""
import argparse
import random
import time

import requests  # pip install requests (not in requirements.txt - this is a standalone example)


def read_sensors():
    """Replace this with a real sensor read. Return a dict of whatever
    fields you have this cycle - you don't need to send all of them every
    time, and a name that doesn't exist yet gets auto-registered."""
    return {
        "temp": round(18 + random.uniform(-0.5, 0.5), 2),
        "turbidity": round(3 + random.uniform(-0.3, 0.3), 2),
        "battery": round(80 + random.uniform(-1, 1), 1),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000", help="base URL of the running telem API")
    p.add_argument("--buoy", default="buoy-01", help="buoy id to feed - must already be registered")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between readings")
    args = p.parse_args()

    url = f"{args.api}/api/ingest/buoys/{args.buoy}"
    print(f"Feeding {args.buoy} every {args.interval}s. Ctrl+C to stop.")
    print("(Fields sent here flip to real immediately - check the dashboard's")
    print(" drawer for this buoy, or GET /api/buoys, to see 'simulated': false.)\n")

    while True:
        reading = read_sensors()
        try:
            resp = requests.post(url, json=reading, timeout=5)
            resp.raise_for_status()
            print(f"[{time.strftime('%H:%M:%S')}] sent {reading} -> {resp.status_code}")
        except requests.RequestException as e:
            print(f"[{time.strftime('%H:%M:%S')}] ingest failed: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
