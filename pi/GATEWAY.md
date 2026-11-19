# RAK2287 Pi HAT (SPI) as the LoRaWAN gateway

**Notes from RAK's and The Things Stack's published files, not a tested procedure.** Nothing here has
been run on the Pi. Verify each step, and keep a backup of the SD card before changing the gateway.

## What the sources say

- RAK's installer (`github.com/RAKWireless/rak_common_for_gateway`, menu item 7, "RAK7248 no LTE (RAK2287 SPI +
  raspberry pi)") has no OS-version check in the files read, and its README points at Raspberry Pi OS Lite
  (32-bit). Whether Bookworm and the Pi 3B+ work is **not stated**.
- On a Pi 3 or 4 it installs **by default** ChirpStack **v3** (network server, application server, gateway
  bridge) with PostgreSQL, Redis and Mosquitto. `sudo ./install.sh --chirpstack=not_install` skips that. Its
  `ap/`, `lte/`, `rak/` and `sysconf/` steps were not read, so what else it changes is unknown.
- It builds Semtech's `sx1302_hal` (V2.0.1) into `/opt/ttn-gateway/packet_forwarder/lora_pkt_fwd/`, copies the
  **EU863-870** `global_conf.json` there and sets `server_address` to `127.0.0.1`. It enables
  `ttn-gateway.service`. The concentrator reset is GPIO 17 (`reset_lgw.sh`, via `gpioset` or sysfs).
- The Things Stack: register a gateway in the console with its EUI; the console can download a
  `global_conf.json`. It prefers **LoRa Basics Station**; the Semtech UDP forwarder is legacy and "will be
  removed in the future". The US915 sub-band 2 plan name, the UDP server address and the ports are not on the
  pages read.

## What that means for this Pi

- Do **not** run RAK's installer unchanged. A local ChirpStack v3 would fight our Mosquitto and ACL, eat RAM a
  1 GB Pi 3B+ does not have, and publishes a JSON shape `lorawan_adapter.py` does not parse (it reads ChirpStack
  v4 and The Things Stack v3).
- **Chosen: build Semtech's `sx1302_hal` directly** (the code RAK's installer builds), with RAK's reset script.

## Procedure (path B)

Sources: Semtech's `sx1302_hal` readmes and configs, RAK's `reset_lgw.sh`, The Things Stack docs. Marked **(unverified)** where no source covers it.

1. `sudo apt install -y git build-essential`
2. `git clone --branch V2.0.1 --depth 1 https://github.com/Lora-net/sx1302_hal.git ~/sx1302_hal`. V2.0.1 is the
   release RAK builds for the RAK2287; the newer V2.1.0 only targets the USB variant.
3. `cd ~/sx1302_hal && make clean all`. The readme says the reset script must sit in the same directory as
   the `lora_pkt_fwd` executable (check `packet_forwarder/` after the build).
4. Replace the reset script (BCM GPIO 17 resets the HAT; Semtech's expects pins 23/18/22/13). RAK's own script
   failed on a Pi 3B+ with current Raspberry Pi OS: `gpioset: invalid line value: 'gpiochip0'`, then `chip
   version is 0x00` and `radio calibration failed`, because the installed `gpioset` is libgpiod 2 and RAK's
   uses the libgpiod 1 command line. Use `pi/gateway/reset_lgw.sh` instead (same low, high, low sequence, via
   `pinctrl`) and put it in `packet_forwarder/` next to `lora_pkt_fwd`.
   **Then apply RAK's patch for this HAT.** The HAT has no I2C temperature sensor (`i2cdetect -y 1` is empty) but
   Semtech's code refuses to start without one (`ERROR: no temeprature sensor found`). RAK's installer
   overwrites Semtech's sources before building. Two of those files carry the fix, and **both are needed**:
   `libloragw/src/loragw_hal.c` (the sensor probe is compiled out) and `libloragw/src/loragw_stts751.c` (the
   driver is a stub that reports success). Download them from
   `https://raw.githubusercontent.com/RAKWireless/rak_common_for_gateway/master/lora/rak2287/` into those paths,
   keep a copy of `reset_lgw.sh`, then `make clean all` and put the script back. RAK's other overrides (GPS,
   Makefile, `lora_pkt_fwd.c`) are not needed; `lora_pkt_fwd.c` also sets the system clock from GPS.
   Side effect: the stub leaves the temperature at 0.0, so RSSI temperature compensation uses 0 C and the
   reported RSSI can be off by a few dB. Transmit power does not use the temperature.
5. `cp global_conf.json.sx1250.US915 in `packet_forwarder/`. The stock file already
   covers 903.9-905.3 MHz plus 904.6 MHz (500 kHz), the US915 sub-band 2 channels. `com_path` is
   `/dev/spidev0.0` as it should be. Edit `gateway_conf`: `gateway_ID`, `server_address`, `serv_port_up`,
   `serv_port_down` (stock: `localhost`, 1730).
6. Gateway EUI: a unique 16-hex-digit value. The usual convention (not in the sources) is the Pi's MAC with
   `fffe` in the middle: `ip link show eth0 | awk '/ether/ {print $2}' | awk -F: '{print $1$2$3"fffe"$4$5$6}'`.
   Semtech's `util_chip_id` can read the concentrator's own EUI instead (see its readme).
7. The Things Stack console: register the gateway with that EUI and frequency plan **`US_902_928_FSB_2`**.
   Use the console's "Download global_conf.json" to read the right `server_address` and ports (unverified:
   believed `nam1.cloud.thethings.network` and 1700) and copy only those values into your config.
8. Run in the foreground first: `sudo ./lora_pkt_fwd -c global_conf.json` from `packet_forwarder/`. When it
   works, turn it into a systemd service.

The Things Stack discourages the Semtech UDP forwarder ("many security and scalability drawbacks", Basics
Station preferred) and a RAK page says UDP support will be removed in future. Use UDP to get packets flowing,
then plan a move to Basics Station.

## Safe to do now

With the Pi powered off, seat the HAT, then:

```bash
sudo raspi-config nonint do_spi 0 && sudo raspi-config nonint do_i2c 0 && sudo reboot
```

After the reboot, `ls /dev/spidev* /dev/i2c*` should list `/dev/spidev0.0` and `/dev/i2c-1`. **I2C is required**:
without it `lora_pkt_fwd` stops with `failed to open I2C for temperature sensor on port 0x39` (it reads the
HAT's temperature sensor at I2C address 0x39). RAK's README also asks for the serial login shell off.

## Seen on the Pi (Pi 3B+, current Raspberry Pi OS)

`lora_pkt_fwd` parsed the US915 config, took the gateway EUI, and opened `/dev/spidev0.0`. With RAK's reset
script it then failed as described in step 4. A `WARNING: impossible to open /dev/ttyS0 for GPS sync` is
harmless without a GPS. With the `pinctrl` reset the chip answered (`chip version is 0x10 (v1.0)`); the next
failure was the missing I2C bus above. If it still fails with `/dev/i2c-1` present, RAK ships patched HAL
sources for this HAT (`loragw_hal.c`, `loragw_stts751.c` in its `lora/rak2287/`), which would then have to be
applied over `sx1302_hal` V2.0.1.

After the reset script and RAK's two patched files were in place, `lora_pkt_fwd` printed
`concentrator started, packet can now be received` (chip version 0x10, concentrator EUI
`0x0016c001f10b1b14`). Register in The Things Stack whichever EUI is in `gateway_ID`. To start it at boot use
`pi/gateway/lora-pkt-fwd.service`; stop any foreground copy first, because only one process can own the SPI bus.
