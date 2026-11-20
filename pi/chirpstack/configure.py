#!/usr/bin/env python3
"""Point a fresh ChirpStack install (SQLite build) at this Pi's Mosquitto and US915 sub-band 2.

    sudo python3 pi/chirpstack/configure.py             # show what would change, write nothing
    sudo python3 pi/chirpstack/configure.py --apply

Edits /etc/chirpstack/chirpstack.toml, region_us915_1.toml and the gateway bridge config (a .orig copy of
each is kept), creates Mosquitto users and ACL rules for chirpstack, gwbridge and lorawan with random
passwords, writes the adapter's password to /etc/default/lorawan-adapter, points the packet forwarder at
the gateway bridge on 127.0.0.1:1700, and restarts the services. Refuses to run twice.
"""

import argparse
import re
import secrets
import shutil
import string
import subprocess
import sys
import tomllib
from pathlib import Path

CHIRPSTACK = Path("/etc/chirpstack/chirpstack.toml")
REGION = Path("/etc/chirpstack/region_us915_1.toml")
BRIDGE = Path("/etc/chirpstack-gateway-bridge/chirpstack-gateway-bridge.toml")
FORWARDER = Path("/home/vitalpi/sx1302_hal/packet_forwarder/global_conf.json")
MOSQ_PASSWD = Path("/etc/mosquitto/passwd")
MOSQ_ACL = Path("/etc/mosquitto/acl")
ADAPTER_ENV = Path("/etc/default/lorawan-adapter")
SQLITE_DB = "/var/lib/chirpstack/chirpstack.sqlite"
MARKER = "# --- ChirpStack (pi/chirpstack/configure.py) ---"
ACL = f"""
{MARKER}
user chirpstack
topic readwrite application/#
topic readwrite us915_1/gateway/#

user gwbridge
topic readwrite us915_1/gateway/#

user lorawan
topic read application/+/device/+/event/up
"""
SERVICES = ["mosquitto", "chirpstack-sqlite", "chirpstack-gateway-bridge", "lora-pkt-fwd"]


def new_password():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))


def sub(text, pattern, repl, label, report, flags=re.M):
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    report.append(f"  {'changed' if n else 'NOT FOUND (edit by hand)'}: {label}")
    return new


def edit_chirpstack(text, pw):
    report = []
    if re.search(r"(?m)^\s*(username|password|qos)\s*=", text):
        raise SystemExit("chirpstack.toml already has MQTT username/password/qos set: restore the .orig first")
    text = sub(text, r"enabled_regions\s*=\s*\[.*?\]", 'enabled_regions = ["us915_1"]', "enabled_regions", report, re.S)
    # qos 1 so Mosquitto can queue uplinks for the adapter while it is down
    text = sub(text, r'(?m)^(\s*)(server = "tcp://localhost:1883/"\n)',
               rf'\1\2\1username = "chirpstack"\n\1password = "{pw}"\n\1qos = 1\n', "[integration.mqtt] login and qos 1", report)
    # the default path is relative, and the unit's working directory is /, which chirpstack cannot write
    if not re.search(r"(?m)^\[sqlite\]", text):
        text += f'\n[sqlite]\n  path="sqlite://{SQLITE_DB}"\n'
        report.append("  changed: [sqlite] path (the default is relative, so ChirpStack could not open its database)")
    return text, report


def edit_region(text, pw):
    report = []
    text = sub(text, r'(?m)^(\s*username\s*=\s*)""', r'\1"chirpstack"', "gateway backend username", report)
    text = sub(text, r'(?m)^(\s*password\s*=\s*)""', rf'\1"{pw}"', "gateway backend password", report)
    return text, report


def edit_bridge(text, pw):
    report = []
    text = sub(text, r'(?m)^(\s*username\s*=\s*)""', r'\1"gwbridge"', "bridge MQTT username", report)
    text = sub(text, r'(?m)^(\s*password\s*=\s*)""', rf'\1"{pw}"', "bridge MQTT password", report)
    # the region's topic_prefix is us915_1, so the bridge must publish under it
    for kind in ("event", "command"):
        text = sub(text, rf'(?m)^(\s*{kind}_topic_template\s*=\s*")gateway/', r"\1us915_1/gateway/", f"{kind} topic prefix", report)
    if re.search(r"(?m)^\s*state_topic_template\s*=", text):
        text = sub(text, r'(?m)^(\s*state_topic_template\s*=\s*")gateway/', r"\1us915_1/gateway/", "state topic prefix", report)
    else:  # the packaged file leaves it out, so the bridge's unprefixed default would apply
        text = sub(text, r'(?m)^(\s*)(event_topic_template\s*=\s*"us915_1/gateway/[^\n]*\n)',
                   r'\1\2\1state_topic_template="us915_1/gateway/{{ .GatewayID }}/state/{{ .StateType }}"\n',
                   "state topic prefix (the key was missing, added)", report)
    text = sub(text, r'udp_bind\s*=\s*"0\.0\.0\.0:1700"', 'udp_bind = "127.0.0.1:1700"', "UDP bound to localhost only", report)
    return text, report


def edit_forwarder(text):
    report = []
    text = sub(text, r'("server_address"\s*:\s*)"[^"]*"', r'\1"127.0.0.1"', "forwarder server_address", report)
    text = sub(text, r'("serv_port_up"\s*:\s*)\d+', r"\g<1>1700", "forwarder serv_port_up", report)
    text = sub(text, r'("serv_port_down"\s*:\s*)\d+', r"\g<1>1700", "forwarder serv_port_down", report)
    return text, report


def run(cmd):
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
    args = ap.parse_args()
    pw = {u: new_password() for u in ("chirpstack", "gwbridge", "lorawan")}

    plan = []
    for path, fn, secret in [(CHIRPSTACK, edit_chirpstack, pw["chirpstack"]), (REGION, edit_region, pw["chirpstack"]),
                             (BRIDGE, edit_bridge, pw["gwbridge"])]:
        new, report = fn(path.read_text(), secret)
        tomllib.loads(new)  # never write a file ChirpStack could not parse
        plan.append((path, new, report))
    if FORWARDER.exists():
        new, report = edit_forwarder(FORWARDER.read_text())
        plan.append((FORWARDER, new, report))
    for path, _, report in plan:
        print(path)
        print("\n".join(report))
    if any("NOT FOUND" in line for _, _, report in plan for line in report):
        sys.exit("Some settings were not found; nothing was written. Send me the file's current lines.")
    if not args.apply:
        print("\nDry run only. Run again with --apply to write these changes.")
        return
    for path, new, _ in plan:
        shutil.copy2(path, path.with_name(path.name + ".orig"))
        path.write_text(new)  # in place, so owner and mode stay as the package set them
    for user, password in pw.items():
        run(["mosquitto_passwd", "-b", str(MOSQ_PASSWD), user, password])
    if MARKER not in MOSQ_ACL.read_text():
        with open(MOSQ_ACL, "a") as f:
            f.write(ACL)
    run(["chown", "root:mosquitto", str(MOSQ_PASSWD), str(MOSQ_ACL)])
    run(["chmod", "640", str(MOSQ_PASSWD), str(MOSQ_ACL)])
    if not ADAPTER_ENV.exists():
        ADAPTER_ENV.write_text(f"LORAWAN_MQTT_PASSWORD={pw['lorawan']}\n")
        ADAPTER_ENV.chmod(0o600)
    run(["install", "-d", "-o", "chirpstack", "-g", "chirpstack", "-m", "750", str(Path(SQLITE_DB).parent)])
    for unit in SERVICES:
        run(["systemctl", "restart", unit])
    print("\nDone. ChirpStack's web UI is on port 8080. Check: journalctl -u chirpstack-sqlite -n 20 --no-pager")


if __name__ == "__main__":
    main()
