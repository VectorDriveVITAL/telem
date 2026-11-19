# MQTT topic plan

One broker (Mosquitto on the Pi), two kinds of thing to publish. Everything lives under `vital/`
so other projects can share the broker.

| Topic | Who publishes | What it carries | QoS | Retain |
|---|---|---|---|---|
| `vital/buoy/<buoy_id>/readings` | the gateway (the bridge reading the LoRa receiver) | one reading from one buoy | 1 | no |
| `vital/service/<name>/metrics` | anything that reports health | `latency_ms`, `rps`, `error_rate` | 1 | no |

`<buoy_id>` and `<name>` must match what is registered in telem (`buoy-01`, `lora-receiver`).
Use lowercase letters, digits and `-`. Never put a unit or a sensor name in the topic.

**One message per reading, JSON, flat.** A reading is one message so its values share a timestamp.
Keys are telem's sensor names; values are plain numbers.

```json
{"time": "2026-11-19T15:25:40Z", "total_dissolved_solids": 10.4, "conductivity": 20.9,
 "temp": 22.4, "air_temp": 22.7, "humidity": 77.1, "core_temp": 22.1, "core_humidity": 73.4,
 "pressure": 1010.4, "ph": 7.5, "turbidity": 0.0, "signal_dbm": -18}
```

- **`time`** is optional ISO 8601 with a zone. Without it the broker-side arrival time is used, so
  include it whenever the gateway can buffer readings (a reading sent late keeps its real time).
  Times more than 5 minutes in the future are refused.
- **Missing key = not measured.** Never send `0` or `null` for a sensor that is off.
- **Numbers only.** Strings and booleans are ignored. Nested objects are flattened with `_`.
- At most 64 keys per reading. Telem's own fields also work: `battery` (0-100), `lat`, `lng`,
  `satellites`, `solar_watts`.

| Key | Meaning | Unit |
|---|---|---|
| `temp` | water temperature (NTC) | °C |
| `air_temp` / `humidity` | outside air (SHT31) | °C / %RH |
| `core_temp` / `core_humidity` | inside the hull (BME280); humidity rising means a leak | °C / %RH |
| `pressure` | barometric pressure (BME280) | hPa |
| `total_dissolved_solids` / `conductivity` | SEN0244 | ppm / µS/cm |
| `ph`, `turbidity` | SEN0161, SEN0189 | pH, NTU |
| `signal_dbm` | LoRa signal strength at the gateway | dBm |

Service example: `vital/service/lora-receiver/metrics` with `{"latency_ms": 5000, "rps": 0.2, "error_rate": 0}`.

## Who may do what (`mosquitto/acl`)

| MQTT user | Publish | Subscribe |
|---|---|---|
| `bridge` (the gateway) | `vital/buoy/+/readings`, `vital/service/+/metrics` | nothing |
| `telegraf` | nothing | `vital/#` |

A device that is compromised can send readings, but cannot read the others or reach other topics.

## Adding a buoy

1. Register it in telem (`POST /api/buoys`, or the dashboard). Telem rejects readings for unknown
   buoys and says so in the log; it does not create them.
2. Publish to `vital/buoy/<new-id>/readings`. No Telegraf or Mosquitto change is needed.

## Reserved, not used yet

`vital/gateway/<id>/status` (retained, with a last-will message) for "is the gateway online".
Add it when something needs to alert on it; the ACL already has no rule for it, so add one then.
Change the whole namespace to `vital/v2/...` if the payload format ever has to break.
