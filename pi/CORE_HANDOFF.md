# Core firmware handoff (Pi side -> core firmware)

The Pi side is built. This page says what it expects from the cores (RAK3172) and lists
the questions we need answered before the first pack can join. Please answer the
**Questions** section by editing this file or replying in chat.

Full payload spec and test vectors: [`LORAWAN.md`](LORAWAN.md).

## What is running on the Pi

| Piece | Detail |
|---|---|
| Gateway | RAK2287 SPI HAT, Semtech sx1302_hal packet forwarder, EUI `b827ebfffe703dfd` |
| Network server | ChirpStack v4.20 on the Pi (self-hosted, no cloud) |
| Region | US915, **sub-band 2 = channels 8-15 (903.9-905.3 MHz) + channel 65 (904.6 MHz)** |
| Topology | Star: every core talks straight to this one gateway. No relaying or mesh |
| Join | OTAA only. Class A |
| Device profile | LoRaWAN **1.0.3**, regional parameters **revision A**. Tell us if your stack differs |
| Application port | **fPort 1** carries sensor data |
| Decoding | Done on the Pi, so send the raw binary payload. No codec on the network server |

## What we need the core to do

1. Join with OTAA, **sub-band 2 only** (channel mask for channels 8-15 + 65). The gateway
   listens nowhere else, so a core on all 64 channels will fail most join attempts.
   RUI3 users: check `AT+MASK` in RAK's docs for the sub-band 2 value (`0002` as far as we know, verify it).
2. Retry the join until it succeeds, with a back-off (for example 10 s, 30 s, 60 s, then every few minutes).
   The first attempts on a fresh sub-band often miss.
3. Send uplinks on **fPort 1** in the payload format below.
4. Send them **unconfirmed** unless there's a reason not to (confirmed uplinks make the gateway transmit,
   which costs airtime and battery).
5. Rejoin automatically if the join is lost (device reset, link check failing).

## Payload v1 (fPort 1)

```
byte 0      version = 1
bytes 1-2   field mask, little-endian uint16. Bit n set = field n is present
bytes 3..   the values of the set bits, in bit order, little-endian
```

| bit | field (telem sensor name) | type | scale (value = raw / scale) |
|---|---|---|---|
| 0 | `temp` | int16 | 100 |
| 1 | `air_temp` | int16 | 100 |
| 2 | `humidity` | uint16 | 100 |
| 3 | `core_temp` | int16 | 100 |
| 4 | `core_humidity` | uint16 | 100 |
| 5 | `pressure` | uint16 | 10 |
| 6 | `total_dissolved_solids` | uint16 | 10 |
| 7 | `conductivity` | uint16 | 10 |
| 8 | `ph` | uint16 | 100 |
| 9 | `turbidity` | uint16 | 10 |
| 10 | `battery` | uint8 | 1 (percent) |
| 11 | `solar_watts` | uint16 | 100 |
| 12 | `age_s` | uint16 | 1 (seconds since the reading was taken) |
| 13 | `lat` | int32 | 10000000 (degrees x 1e7, GNSS) |
| 14 | `lng` | int32 | 10000000 (degrees x 1e7, GNSS) |
| 15 | `satellites` | uint8 | 1 |

- A sensor that is not fitted, not ready or failed: **clear its bit**. Never send 0 or a sentinel value.
- Send the raw (uncalibrated) value. telem applies calibration.
- Maximum 37 bytes (all 16 fields). US915 at DR0 only carries **11 bytes** of application data, DR1 and above carry 53.
  See question 8.
- Signal strength (`signal_dbm`) comes from the gateway, so don't send it.

Test vectors (decode these in your own code to confirm you match):

| hex | meaning |
|---|---|
| `010504ca08d41257` | temp 22.5, humidity 48.2, battery 87 |
| `010100f3fd` | temp -5.25 |
| `01ff07c008de081e1ea208ac1c78276800d100ee02000057` | 11 sensors (see `LORAWAN.md`) |
| `0100e0ab14bd1114b927c709` | lat 29.7604267, lng -95.3698028, 9 satellites |

## What we will give you

- The **AppKey** for each core (we'll generate it in ChirpStack and send it privately, not in git or chat history that others can read). Or, if you'd rather burn your own keys, send us yours.
- The **JoinEUI** to use (we can use `0000000000000000`, or anything you like, as long as both sides match).
- A device name and buoy ID for each core, so readings land on the right buoy in the dashboard.

## Questions

Please fill in the blank after each one. "Don't know yet" is a fine answer. Write what you'd do.

**Hardware and stack**
1. Exact module: RAK3172 or RAK3172-SiP? Variant or hardware revision?
   -
2. What runs on it: RAK's RUI3 firmware (AT commands over UART from a host MCU), or your own firmware on the STM32WLE5 (STM32CubeWL, LoRaMac-node, other)?
   -
3. LoRaWAN stack version. In RUI3, run the version AT command. In LoRaMac-node, find `LORAMAC_VERSION` or the equivalent define.
   -
4. Regional parameters revision the stack implements (RP002-1.0.x)?
   -
5. If a host MCU drives the module over AT commands: which MCU, and which of these tasks does it do: sensor reads, packing the payload, sleeping the radio?
   -

**Identity and keys**
6. DevEUI for each core. It's printed on the module or readable with the AT command. Send all of them.
   -
7. How are keys provisioned: flashed at build, set by AT command at first boot, or stored in flash? Can you change them without reflashing?
   -
8. How many cores exist now, and how many will exist in the field?
   -

**Radio settings**
9. Is the channel mask set to sub-band 2 (channels 8-15 + 65)? How?
   -
10. Data rate plan. Options: ADR on (the network picks the rate), or fixed DR (for example DR3 for a 242-byte limit, short airtime). Which does the firmware use? **If ADR is on, uplinks may drop to DR0, and the 28-byte payload won't fit (11 bytes max)**. Either fix a rate at DR1 or higher, or keep payloads at 11 bytes or less. What is your choice?
    -
11. Transmit power setting and the antenna fitted (gain, connector)?
    -
12. Join retry and rejoin behavior as in "What we need the core to do". What does the firmware do today?
    -
13. Are uplinks unconfirmed? Do you need any downlinks at all (for example remote reset or interval change)? If yes, say what, since it affects how we configure ChirpStack.
    -

**Data**
14. Which sensors does each core actually send, from the table above? List by core if they differ. Any sensor not in the table?
    -
15. Are the units and scales in the table right for your readings (for example conductivity in µS/cm, TDS in ppm)? Correct anything that isn't.
    -
16. Is the value sent raw, or already smoothed or calibrated on the core?
    -
17. Do you send `age_s`? How old can a reading be when sent? Or send nothing and we use the receive time.
    -
18. Battery: is it a percentage (0-100) or a voltage? Do you have a battery curve we should know about?
    -

**Timing and power**
19. Uplink interval. Fixed, or does it change (faster on alert, slower on low battery)?
    -
20. Does the core sleep between uplinks? Roughly how long does a join take on power-up?
    -
21. How are failures reported: a status or error flag in the payload, or silence? (We treat silence as offline after a timeout; tell us how long it should be.)
    -

**Testing**
22. When can you do a first join test next to the Pi? We'll watch the gateway's **LoRaWAN frames** page live while you power on.
    -
23. Can you send one known payload (for example the first test vector `010504ca08d41257`) so we can check the decode end to end?
    -
24. Anything about your firmware we should know that I haven't asked? Firmware version string, a known bug, a feature you want on the Pi side (for example sending an alert on low battery)?
    -

## What we check when you power on a core

1. ChirpStack gateway page, **LoRaWAN frames**: a JoinRequest then a JoinAccept.
   - JoinRequest but no JoinAccept: wrong keys or EUIs, or a LoRaWAN version mismatch.
   - No frames at all: wrong channel mask, or the core isn't transmitting.
2. The device page, **Events**: uplinks with `data` in base64.
3. The telem dashboard: the buoy's readings and signal strength update.
