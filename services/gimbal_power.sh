#!/bin/bash
# Power the gimbal (GPIO 17 high) once psdk_telemetry reports the accepted 24 V request
if grep -q -m1 "high power requested" \
        < <(timeout 120 journalctl -f -n all -o cat _SYSTEMD_INVOCATION_ID="$INVOCATION_ID"); then
    pinctrl set 17 op dh
fi
exit 0