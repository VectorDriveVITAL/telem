# LoRaWAN sensor packs

```
sensor pack ~~LoRaWAN~~> RAK2287 HAT -> network server -> MQTT -> lorawan_adapter.py -> telem REST
```

The HAT only receives radio frames. A network server (ChirpStack or The Things Stack) decrypts them and
publishes JSON over MQTT. `lorawan_adapter.py` reads that, decodes the pack's bytes itself and POSTs a
reading to `/api/ingest/buoys/<buoy>`, so calibration, alerts and silent-pack detection all apply.
**The adapter decodes the payload, so the firmware does not depend on which network server you choose.**
Star topology: every pack talks only to the one gateway; each pack is its own source in telem.

## Decisions to confirm

| Decision | Choice | Why |
|---|---|---|
| Band | US915, sub-band 2 (channels 8-15) | assumed from the 915 MHz P2P link; the gateway and every pack must use the same sub-band |
| Activation | OTAA, one DevEUI and AppKey per pack | never share keys between packs |
| Class / uplinks | Class A, unconfirmed, fPort 1 | the packs only report; nothing is sent back |
| Interval | pick per need (60 s is fine) | a self-hosted network server has no fair-use limit; only the regulatory dwell time applies |
| Time | no clock on the pack | the network server stamps arrival; buffered readings carry `age_s` |
| Network server | **ChirpStack v4 on the Pi** (self-hosted) | decided: the project is self-hosted, so no cloud account, no internet dependency, no fair-use limit |

**Network server.** ChirpStack on the Pi, with the Semtech packet forwarder feeding the ChirpStack Gateway Bridge
on `127.0.0.1:1700`, the bridge and ChirpStack sharing the Pi's Mosquitto, and US915 sub-band 2 as region
`us915_1` (channels 8-15 plus 65). It needs Redis and either PostgreSQL or the lighter SQLite build
(`chirpstack-sqlite`). Memory use on a 1 GB Pi 3B+ is not documented: measure it. The Things Stack in the cloud
remains possible by changing only the adapter's broker, topic and credentials, but its Sandbox limits each
device to 30 s of uplink airtime a day, and its MQTT is QoS 0 with no retention.

## Payload v1 (fPort 1)

Byte 0 is the version (`01`). Bytes 1-2 are a little-endian bit mask of the fields present. Then each
present field follows in bit order, little-endian. **A field that is not in the mask was not measured**:
never send 0 for a sensor that is off. Never reorder bits; only append, and bump the version if a field's
meaning changes.

| Bit | Field (telem name) | Type | Scale | Unit |
|---|---|---|---|---|
| 0 | `temp` (water) | int16 | /100 | °C |
| 1 | `air_temp` | int16 | /100 | °C |
| 2 | `humidity` | uint16 | /100 | %RH |
| 3 | `core_temp` (inside the hull) | int16 | /100 | °C |
| 4 | `core_humidity` | uint16 | /100 | %RH |
| 5 | `pressure` | uint16 | /10 | hPa |
| 6 | `total_dissolved_solids` | uint16 | /10 | ppm |
| 7 | `conductivity` | uint16 | /10 | µS/cm |
| 8 | `ph` | uint16 | /100 | pH |
| 9 | `turbidity` | uint16 | /10 | NTU |
| 10 | `battery` | uint8 | 1 | % (0-100) |
| 11 | `solar_watts` | uint16 | /100 | W |
| 12 | `age_s` | uint16 | 1 | s since measured (0 or absent = just now) |
| 13 | `lat` | int32 | /1e7 | degrees (GNSS; clear bits 13-15 when there is no fix) |
| 14 | `lng` | int32 | /1e7 | degrees |
| 15 | `satellites` | uint8 | 1 | satellites in the fix |

The 16-bit mask is now full, so the next new field needs payload version 2.

Largest payload (all 16 fields) is 37 bytes. US915 allows 53 bytes at DR1 and above, but only **11 at DR0**,
so a pack that falls back to DR0 cannot send a full reading. Keep ADR on and the packs within DR1 range.

Test vectors (also in `tests/test_lorawan_adapter.py`; the firmware must produce exactly these bytes):

| Input | Bytes |
|---|---|
| temp 22.5, humidity 48.2, battery 87 | `010504ca08d41257` |
| temp 22.4, air_temp 22.7, humidity 77.1, core_temp 22.1, core_humidity 73.4, pressure 1010.4, tds 10.4, conductivity 20.9, ph 7.5, turbidity 0.0, battery 87 | `01ff07c008de081e1ea208ac1c78276800d100ee02000057` |
| temp -5.25 | `010100f3fd` |

C packing, no library needed: write `(int16_t)lroundf(value * 100)` with `memcpy` (Cortex-M0+ is little-endian),
setting the mask bit only when the sensor read succeeded.

## Firmware checklist

- LoRaWAN stack with OTAA in US915, sub-band 2. If a pack uses a RAK3272S, it is AT-command driven:
  `AT+NWM`, `AT+NJM`, `AT+BAND`, `AT+MASK`, `AT+DEVEUI`/`AT+APPEUI`/`AT+APPKEY`, `AT+ADR`, `AT+JOIN`, then
  `AT+SEND=1:<hex>`. Check the exact syntax against RAK's AT manual for your firmware version.
- Send on fPort 1, unconfirmed. Reuse the existing sensor-reading code; only the radio layer changes.
- Use `age_s` only if a pack stores readings while it cannot join.
- Record each pack's DevEUI in `devices.json` next to its buoy id.

## Gateway (RAK2287)

Use the SPI HAT with SPI enabled on the Pi. It is SX1302-based; ChirpStack's Concentratord has a build for
it. Set the gateway's channel plan to US915 sub-band 2 to match the packs. Follow RAK's RAK2287 guide for the
packet forwarder or Concentratord setup, which I have not verified against your Pi image.

## Adapter

```bash
pip install -r pi/requirements.txt
cp pi/devices.example.json /etc/telem/devices.json      # DevEUI -> buoy id; register each buoy in telem first
sudo cp pi/lorawan-adapter.service /etc/systemd/system/ # edit the paths
echo 'LORAWAN_MQTT_PASSWORD=...' | sudo tee /etc/default/lorawan-adapter
sudo systemctl enable --now lorawan-adapter
```

- ChirpStack on this Pi: default topic `application/+/device/+/event/up`; add the `lorawan` user to the Mosquitto
  ACL (commented example in `pi/mosquitto/acl`).
- The Things Stack: `--broker <your cluster address> --port 8883 --tls --user <application id> --topic 'v3/+/devices/+/up'`,
  with an API key as `LORAWAN_MQTT_PASSWORD`.
- With a local ChirpStack, set `qos=1` in its `[integration.mqtt]` section (default 0). The adapter's persistent
  QoS 1 subscription then lets Mosquitto queue uplinks while the adapter is down. The Things Stack's MQTT is
  QoS 0 only and keeps nothing; there the username is `<application id>@<tenant id>`, the password an API key,
  and uplinks are on `v3/<application id>@<tenant id>/devices/<device id>/up`.
- Failed posts retry, then spool to `spool.ndjson` and replay every 30 s. A reading telem rejects (unknown buoy,
  bad value) is logged and dropped.

Try the Pi side with no gateway, network server or broker at all. Register a buoy for the pack, map its DevEUI to
it, and push one fake uplink through:

```bash
curl -s -X POST localhost:8000/api/buoys -H 'Content-Type: application/json' -d '{"id":"core-01","lat":29.7,"lng":-95.4,"sensors":["temp"]}'
echo '{"70b3d57ed0012345": "core-01"}' > /tmp/devices.json
python3 pi/lorawan_adapter.py --print-sample 70b3d57ed0012345 temp=22.5 battery=87 | python3 pi/lorawan_adapter.py --once --devices /tmp/devices.json
```

Or, with a broker in the path:

```bash
python3 pi/lorawan_adapter.py --print-sample 70b3d57ed0012345 temp=22.5 humidity=48.2 battery=87 > /tmp/up.json
mosquitto_pub -h 127.0.0.1 -u lorawan -P '<password>' -t application/1/device/70b3d57ed0012345/event/up -f /tmp/up.json
```

Message shapes are ChirpStack v4's and The Things Stack v3's as documented; confirm against one real uplink
(`mosquitto_sub -t '#' -v -C 1`) once a network server is running.
