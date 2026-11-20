#!/usr/bin/env python3
"""LoRaWAN uplinks over MQTT -> telem's REST API.

The network server (ChirpStack v4 or The Things Stack v3) decrypts each uplink and publishes it
as JSON. This reads those messages, decodes the sensor pack's binary payload (layout in
pi/LORAWAN.md), and POSTs one reading per uplink to /api/ingest/buoys/<buoy>. Going through the
REST API means calibration, alerts, silent-buoy detection and VITAL forwarding all apply.

    LORAWAN_MQTT_PASSWORD=... python3 pi/lorawan_adapter.py --devices /etc/telem/devices.json
    python3 pi/lorawan_adapter.py --print-sample 70b3d57ed0012345 temp=22.5 humidity=48.2
    python3 pi/lorawan_adapter.py --print-sample 70b3d57ed0012345 temp=22.5 | python3 pi/lorawan_adapter.py --once

Needs `pip install paho-mqtt` (the decoding and posting need nothing).
"""

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Payload v1: byte 0 = version (1), bytes 1-2 = little-endian bit mask of which fields follow,
# then each present field in bit order. Little-endian so a Cortex-M can memcpy its values.
# Bit order is the contract with the firmware: never reorder, only append.
FIELDS = [  # (telem sensor name, struct format, divisor)
    ("temp", "<h", 100),                    # bit 0   water, degC
    ("air_temp", "<h", 100),                # bit 1   degC
    ("humidity", "<H", 100),                # bit 2   %RH
    ("core_temp", "<h", 100),               # bit 3   inside the hull, degC
    ("core_humidity", "<H", 100),           # bit 4   inside the hull, %RH
    ("pressure", "<H", 10),                 # bit 5   hPa
    ("total_dissolved_solids", "<H", 10),   # bit 6   ppm
    ("conductivity", "<H", 10),             # bit 7   uS/cm
    ("ph", "<H", 100),                      # bit 8
    ("turbidity", "<H", 10),                # bit 9   NTU
    ("battery", "<B", 1),                   # bit 10  percent, 0-100
    ("solar_watts", "<H", 100),             # bit 11  W
    ("age_s", "<H", 1),                     # bit 12  seconds since measured (buffered readings)
    ("lat", "<i", 10_000_000),              # bit 13  degrees, int32 x 1e7 (GNSS)
    ("lng", "<i", 10_000_000),              # bit 14  degrees, int32 x 1e7 (GNSS)
    ("satellites", "<B", 1),                # bit 15  satellites in the fix. The 16-bit mask is now full: a new field means payload version 2
]
VERSION = 1
READINGS_PORT = 1
RETRIES = 4
SPOOL_REPLAY_S = 30


def decode(raw):
    if len(raw) < 3 or raw[0] != VERSION:
        raise ValueError(f"unsupported payload (version {raw[0] if raw else '-'}, {len(raw)} bytes)")
    mask = int.from_bytes(raw[1:3], "little")
    out, pos = {}, 3
    for bit, (name, fmt, divisor) in enumerate(FIELDS):
        if mask >> bit & 1:
            size = struct.calcsize(fmt)
            if pos + size > len(raw):
                raise ValueError("payload is shorter than its field mask says")
            (value,) = struct.unpack_from(fmt, raw, pos)
            out[name] = value / divisor if divisor != 1 else value
            pos += size
    if pos != len(raw):
        raise ValueError("payload has bytes after the last field")
    return out


def encode(**values):
    """Reference encoder (the firmware does the same in C); used by tests and --print-sample."""
    mask, body = 0, b""
    for bit, (name, fmt, divisor) in enumerate(FIELDS):
        if values.get(name) is not None:
            mask |= 1 << bit
            body += struct.pack(fmt, round(values[name] * divisor))
    return bytes([VERSION]) + mask.to_bytes(2, "little") + body


def _best(rx_list):
    return max(rx_list or [{}], key=lambda r: r.get("rssi", -999))  # the gateway that heard it best


def parse_uplink(msg):
    """Normalise a ChirpStack v4 or The Things Stack v3 uplink to one shape."""
    if "deviceInfo" in msg:
        rx = _best(msg.get("rxInfo"))
        return {"dev_eui": msg["deviceInfo"]["devEui"].lower(), "f_port": msg.get("fPort"),
                "f_cnt": msg.get("fCnt"), "data": base64.b64decode(msg.get("data") or ""),
                "time": msg.get("time"), "rssi": rx.get("rssi"), "snr": rx.get("snr")}
    if "uplink_message" in msg:
        up, rx = msg["uplink_message"], _best(msg["uplink_message"].get("rx_metadata"))
        return {"dev_eui": (msg["end_device_ids"].get("dev_eui") or "").lower(), "f_port": up.get("f_port"),
                "f_cnt": up.get("f_cnt"), "data": base64.b64decode(up.get("frm_payload") or ""),
                "time": msg.get("received_at"), "rssi": rx.get("rssi"), "snr": rx.get("snr")}
    raise ValueError("not a ChirpStack or The Things Stack uplink")


def _when(stamp, age_s):
    """ISO time telem accepts: Python's parser wants at most 6 fractional digits and no Z."""
    if not stamp:
        return None
    dt = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", stamp).replace("Z", "+00:00"))
    return (dt - timedelta(seconds=age_s or 0)).astimezone(timezone.utc).isoformat()


def build_body(up, fields):
    fields = dict(fields)
    age = fields.pop("age_s", None)
    body = dict(fields)
    if up["rssi"] is not None:
        body["signal_dbm"] = up["rssi"]  # what the gateway heard, telem's buoy link strength
    if (t := _when(up["time"], age)) is not None:
        body["time"] = t
    return body


def handle(payload, devices, send, seen=None, link=None):
    """One MQTT message in; `send(buoy, body)` is called for a reading, `link(record)` for its
    link quality (also for devices not in the file, so the dashboard shows them). Returns a log line."""
    up = parse_uplink(json.loads(payload))
    if up["f_port"] != READINGS_PORT:
        return f"ignored  {up['dev_eui']}  port {up['f_port']}"
    buoy = devices.get(up["dev_eui"])
    if link:
        link({"dev_eui": up["dev_eui"], "buoy": buoy, "f_cnt": up["f_cnt"], "rssi": up["rssi"], "snr": up["snr"]})
    if buoy is None:
        if seen is None or up["dev_eui"] not in seen:
            (seen if seen is not None else set()).add(up["dev_eui"])
            return f"unknown device {up['dev_eui']}: add it to the devices file to ingest it"
        return ""
    fields = decode(up["data"])
    send(buoy, build_body(up, fields))
    return (f"uplink   {buoy}  fCnt {up['f_cnt']}  rssi {up['rssi']}  snr {up['snr']}  "
            f"{len(fields)} fields")


def post(telem, buoy, body):
    """Returns "ok", "retry" (telem or the network is down) or "drop" (the reading is wrong)."""
    req = urllib.request.Request(
        f"{telem.rstrip('/')}/api/ingest/buoys/{buoy}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return "ok"
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return "retry"
        print(f"  rejected by telem for {buoy}: HTTP {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}")
        return "drop"  # a 4xx will not get better by trying again
    except (urllib.error.URLError, TimeoutError, OSError):
        return "retry"


def post_link(telem, record):
    """Best effort: link statistics are nice to have, so a failure is never retried or spooled."""
    req = urllib.request.Request(f"{telem.rstrip('/')}/api/lorawan/uplink", data=json.dumps(record).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).close()
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


class Sender:
    """Retry briefly, then spool to disk and replay later, so a telem restart loses nothing."""

    def __init__(self, telem, spool):
        self.telem, self.spool = telem, Path(spool)

    def __call__(self, buoy, body):
        for attempt in range(RETRIES):
            result = post(self.telem, buoy, body)
            if result != "retry":
                return
            time.sleep(2 ** attempt)
        with open(self.spool, "a", encoding="utf-8") as f:
            f.write(json.dumps({"buoy": buoy, "body": body}) + "\n")
        print(f"  telem unreachable: spooled a reading for {buoy}")

    def replay(self):
        if not self.spool.exists() or not self.spool.stat().st_size:
            return
        kept = []
        for line in self.spool.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if kept or post(self.telem, item["buoy"], item["body"]) == "retry":
                kept.append(line)  # still down: keep this and everything after it, in order
        self.spool.write_text("".join(l + "\n" for l in kept), encoding="utf-8")


def sample_uplink(dev_eui, **values):
    """A ChirpStack-shaped uplink, for trying the chain before a pack exists."""
    return {"deviceInfo": {"devEui": dev_eui}, "fPort": READINGS_PORT, "fCnt": 1,
            "time": datetime.now(timezone.utc).isoformat(), "rxInfo": [{"rssi": -80, "snr": 9.5}],
            "data": base64.b64encode(encode(**values)).decode()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--tls", action="store_true", help="for a remote broker such as The Things Stack (port 8883)")
    ap.add_argument("--user", default="lorawan")
    ap.add_argument("--topic", default="application/+/device/+/event/up",
                    help="ChirpStack default; The Things Stack uses v3/+/devices/+/up")
    ap.add_argument("--client-id", default="lorawan-adapter")
    ap.add_argument("--telem", default="http://127.0.0.1:8000")
    ap.add_argument("--devices", default="devices.json", help='JSON map of DevEUI to buoy id: {"70b3d5...": "buoy-02"}')
    ap.add_argument("--spool", default="spool.ndjson")
    ap.add_argument("--print-sample", nargs="+", metavar=("DEVEUI", "FIELD=VALUE"),
                    help="print a sample uplink for mosquitto_pub and exit")
    ap.add_argument("--once", action="store_true",
                    help="read one uplink JSON from stdin, send it to telem and exit (no MQTT needed)")
    args = ap.parse_args()

    if args.print_sample:
        values = {k: float(v) for k, v in (kv.split("=") for kv in args.print_sample[1:])}
        print(json.dumps(sample_uplink(args.print_sample[0].lower(), **values)))
        return 0

    if args.once:
        devices = {k.lower(): v for k, v in json.loads(Path(args.devices).read_text()).items()}
        try:
            line = handle(sys.stdin.read(), devices, Sender(args.telem, args.spool), link=lambda r: post_link(args.telem, r))
        except (ValueError, KeyError) as exc:
            print(f"skipped: {exc}")
            return 1
        print(line or "ok")
        return 1 if line.startswith(("unknown device", "ignored")) else 0

    import paho.mqtt.client as mqtt  # imported here so the rest works without it

    devices = {k.lower(): v for k, v in json.loads(Path(args.devices).read_text()).items()}
    send, seen = Sender(args.telem, args.spool), set()

    def on_connect(client, userdata, flags, reason, properties=None):
        print(f"connected to {args.broker}:{args.port} ({reason}); subscribing to {args.topic}", flush=True)
        client.subscribe(args.topic, qos=1)

    def on_message(client, userdata, msg):
        try:
            line = handle(msg.payload, devices, send, seen, lambda r: post_link(args.telem, r))
        except (ValueError, KeyError) as exc:  # one malformed uplink must not stop the rest
            line = f"skipped a message on {msg.topic}: {exc}"
        if line:
            print(f"{time.strftime('%H:%M:%S')}  {line}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id, clean_session=False)
    client.username_pw_set(args.user, os.environ.get("LORAWAN_MQTT_PASSWORD", ""))
    if args.tls:
        client.tls_set()
    client.on_connect, client.on_message = on_connect, on_message
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()
    try:
        while True:
            time.sleep(SPOOL_REPLAY_S)
            send.replay()
    except KeyboardInterrupt:
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
