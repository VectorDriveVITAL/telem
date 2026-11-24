#!/usr/bin/env python3
"""Report this Pi's real services to telem's Service Health tab (stdlib only).

Every INTERVAL seconds, for each systemd unit below: `systemctl is-active`, plus the TCP connect time
where the unit has a port, and a requests-per-second rate for every unit except telegraf and lora-pkt-fwd. latency_ms is that connect time; units without a port report 0 while active.
A unit that is not active or not accepting connections reports DOWN_MS, which telem shows as critical.
"""
import argparse
import json
import os
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
# Units whose request count we can read: unit -> function returning a running total (or None).
# telegraf and lora-pkt-fwd have none yet.
def _get(url):
    return urllib.request.urlopen(url, timeout=3).read().decode()


def prom_total(url, selector):
    """Sum the sample values of the lines in a Prometheus text page that start with `selector`."""
    return sum(float(line.rsplit(" ", 1)[1]) for line in _get(url).splitlines() if line.startswith(selector))


def influx_total():
    return prom_total("http://127.0.0.1:8086/metrics", "http_api_requests_total{")


def chirpstack_total():  # uplink frames the gateways delivered (enabled by [monitoring] in chirpstack.toml)
    return prom_total("http://127.0.0.1:9000/metrics", 'gateway_backend_mqtt_events_total{event="up"}')


def bridge_total():  # PushData also carries the forwarder's 30 s status reports, so this runs above the uplink rate
    return prom_total("http://127.0.0.1:9001/metrics", 'backend_semtechudp_udp_received_count{packet_type="PushData"}')


def telem_total(base):
    return json.loads(_get(base + "/api/health"))["requests_total"]


def adapter_total(base):  # uplinks the adapter has decoded and reported since telem started
    return sum(d["uplinks"] for d in json.loads(_get(base + "/api/lorawan"))["devices"])


def mosquitto_total():
    """Messages the broker has published, from its $SYS stats (a read-only `sysmon` user; see pi/README.md)."""
    import threading
    import paho.mqtt.client as mqtt  # in the telem venv; imported here so the rest runs without it

    seen, got = [], threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set("sysmon", os.environ["MQTT_SYS_PASSWORD"])
    c.on_connect = lambda *a: c.subscribe("$SYS/broker/publish/messages/received")
    c.on_message = lambda cl, u, m: (seen.append(float(m.payload)), got.set())
    c.connect("127.0.0.1", 1883, keepalive=15)
    c.loop_start()
    try:
        got.wait(11)  # $SYS topics are retained and refresh every 10 s
    finally:
        c.loop_stop()
        c.disconnect()
    return seen[0] if seen else None


def rate(prev, total, now):
    """(per-second rate, new state). No rate on the first sample, a missing total or a counter reset."""
    if total is None:
        return None, None
    if prev is None or total < prev[0]:
        return None, (total, now)
    return (total - prev[0]) / (now - prev[1]), (total, now)


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
    totals = {"influxdb": influx_total, "mosquitto": mosquitto_total,
              "chirpstack-sqlite": chirpstack_total, "chirpstack-gateway-bridge": bridge_total, "telem": lambda: telem_total(args.telem),
              "lorawan-adapter": lambda: adapter_total(args.telem)}
    last = {}
    while True:
        for unit, port in UNITS.items():
            latency, err = check(unit, port)
            body = {"latency_ms": latency, "error_rate": err}
            if unit in totals:
                try:
                    total = totals[unit]()
                except (OSError, ValueError, KeyError, ImportError):
                    total = None
                per_s, last[unit] = rate(last.get(unit), total, time.monotonic())
                if per_s is not None:
                    body["rps"] = round(per_s, 4)
            try:
                post(f"{args.telem}/api/services", {"name": unit, "warn_ms": 500, "crit_ms": 1000})
                post(f"{args.telem}/api/ingest/services/{unit}", body)
            except (urllib.error.URLError, OSError) as e:
                print(f"{unit}: telem unreachable: {e}", flush=True)
                break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
