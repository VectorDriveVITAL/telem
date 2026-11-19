# Pi setup: Mosquitto, Telegraf, InfluxDB and telem

```
gateway (bridge) --MQTT--> Mosquitto --> Telegraf --HTTP--> telem --> InfluxDB
                 vital/buoy/<id>/readings        /api/ingest/telegraf
```

LoRaWAN sensor packs arrive through a different path: see [LORAWAN.md](LORAWAN.md). The Telegraf route
below is for anything that publishes to our own `vital/...` topics and is optional if nothing does.

Telegraf does not write InfluxDB. It hands readings to telem, which does, so sensors are
registered, calibration applied, alerts raised and a silent buoy noticed. Topics and payloads:
[TOPICS.md](TOPICS.md).

These files have not been run on a Pi yet. Check each step's output as you go.

## 1. Install (as in the Pi guide: steps 0, 1 and 2)

`mosquitto`, `mosquitto-clients`, then `influxdb2` and `telegraf` from the InfluxData repo.
Skip the guide's Mosquitto and Telegraf config: use the files here instead.

InfluxDB first-run setup: use **org `telem`** and **bucket `telemetry`** (telem's defaults) and keep
the token for telem's `.env` (`INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET`).

## 2. Mosquitto

```bash
sudo cp pi/mosquitto/vital.conf /etc/mosquitto/conf.d/vital.conf
sudo cp pi/mosquitto/acl /etc/mosquitto/acl
sudo mosquitto_passwd -c /etc/mosquitto/passwd bridge      # -c creates the file: first user only
sudo mosquitto_passwd /etc/mosquitto/passwd telegraf       # no -c, or the first user is wiped
sudo chown mosquitto: /etc/mosquitto/passwd /etc/mosquitto/acl
sudo chmod 640 /etc/mosquitto/passwd /etc/mosquitto/acl
sudo systemctl restart mosquitto
```

## 3. Telegraf

```bash
sudo mv /etc/telegraf/telegraf.conf /etc/telegraf/telegraf.conf.dist   # the stock file has its own outputs
sudo cp pi/telegraf/telegraf.conf /etc/telegraf/telegraf.conf
sudo cp pi/telegraf/telegraf.env.example /etc/default/telegraf
sudo nano /etc/default/telegraf        # the telegraf user's MQTT password; TELEM_URL if telem is elsewhere
sudo chown root:telegraf /etc/default/telegraf && sudo chmod 640 /etc/default/telegraf
```

Try it in the foreground with debug output before enabling the service:

```bash
sudo systemctl stop telegraf
sudo -u telegraf bash -c 'set -a; . /etc/default/telegraf; telegraf --config /etc/telegraf/telegraf.conf --debug'
```

## 4. Test end to end

Telem must be running with the buoy registered. In a second terminal on the Pi:

```bash
mosquitto_pub -h 127.0.0.1 -u bridge -P '<bridge password>' -q 1 \
  -t vital/buoy/buoy-01/readings -m '{"temp": 22.5, "humidity": 48.2}'
```

Telegraf's debug output should show a batch written to the HTTP output. Then:

```bash
curl -s localhost:8000/api/buoys | python3 -m json.tool | grep -A3 '"temp"'   # "simulated": false
```

Once it works: `sudo systemctl enable --now telegraf`.

## If it does not work

| Symptom | Likely cause |
|---|---|
| Telegraf: `not authorized` / connection refused | wrong MQTT password, or the ACL file is not readable by `mosquitto` |
| `mosquitto_pub` succeeds, nothing in Telegraf | the ACL does not let that user write the topic, or the topic is not under `vital/` |
| Telegraf writes, telem shows nothing | read telem's console for `[ingest] telegraf metric 0 rejected: ...`; usually the buoy is not registered |
| Readings arrive with the wrong time | the payload `time` is not ISO 8601 with a zone; Telegraf then uses arrival time |
| Changed `topics` but old ones are still used | `persistent_session` keeps the first subscription: change `client_id` once |
| Telegraf starts but ignores `/etc/default/telegraf` | `sudo systemctl cat telegraf` should show an `EnvironmentFile=` line for it |

On the Pi 3B+, InfluxDB uses a few hundred MB of the 1 GB; add swap if Telegraf or Influx gets killed.
