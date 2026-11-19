#!/bin/bash
# Reset the RAK2287 Pi HAT's SX1302 (BCM GPIO 17) for Semtech's lora_pkt_fwd, which calls this with
# "start" and "stop" from its own directory. RAK's script drives the pin with gpioset, whose command
# line differs between libgpiod 1 and 2 ("invalid line value: 'gpiochip0'" on current Raspberry Pi OS),
# so this uses pinctrl. Same sequence as RAK's: low, high, low, 0.1 s apart.
case "$1" in
  start)
    pinctrl set 17 op dl; sleep 0.1
    pinctrl set 17 op dh; sleep 0.1
    pinctrl set 17 op dl; sleep 0.1
    echo "[INFO] SX1302 reset on GPIO 17 done"
    ;;
  stop) ;;   # leave the pin as it is
  *) echo "usage: $0 start|stop" >&2; exit 1 ;;
esac
exit 0
