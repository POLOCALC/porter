#!/usr/bin/env python3
"""
powerd.py  -  battery power/voltage monitoring daemon.

Owns the INA228 power monitor. A single thread does all I2C I/O (reads bus
voltage, current, power, temperature, energy on an interval) and atomically
writes the latest values to a JSON status file. Any other process (e.g. the
separate telemetry script) reads that file directly - no socket, no command
protocol. This daemon is read-only from the outside.

The status file lives on tmpfs (/dev/shm) rather than the SD card, since
it's rewritten every POLL_INTERVAL seconds for the life of the process and
doesn't need to survive a reboot.

Status file format (single JSON object):
{
  "ok": true,
  "reading": {"bus_voltage": .., "shunt_voltage": .., "current": ..,
              "power": .., "temperature": .., "energy": ..},
  "alarms": {"OVERCURRENT": true, ...},
  "error": null,
  "timestamp": 1737480000.123
}

Readers should treat the file as stale (daemon dead/hung) if "timestamp" is
older than a few multiples of POLL_INTERVAL.
"""

import json
import logging
import os
import signal
import threading
import time

from ina228 import INA228

# configuration
I2C_ADDRESS = 0x40
I2C_BUS = 1
SHUNT_RESISTANCE = 0.02   # ohms
MAX_CURRENT = 10.0        # amps (peak)

POLL_INTERVAL = 1.0                       # seconds between INA228 reads
STATUS_PATH = "/tmp/powerd_status.json"
STATUS_TMP_PATH = STATUS_PATH + ".tmp"

# default alarm thresholds for a 6S LiIon (21.6V nominal, 3.6V/cell)
DEFAULT_LIMITS = {
    "bus_voltage_min": 18.0,   # 3.0V/cell - low battery warning
    "bus_voltage_max": 25.2,   # 4.2V/cell - full charge
    "current_max": 10.0,       # amps, matches MAX_CURRENT calibration
}

# logging
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_stream_handler])
logger = logging.getLogger("powerd")

_shutdown = threading.Event()


def _check_alarms(reading: dict) -> dict:
    """Compare a reading against fixed limits, return active alarm flags."""
    alarms = {}
    bv = reading.get("bus_voltage")
    cur = reading.get("current")
    if bv is not None:
        if bv < DEFAULT_LIMITS["bus_voltage_min"]:
            alarms["UNDERVOLTAGE"] = True
        if bv > DEFAULT_LIMITS["bus_voltage_max"]:
            alarms["OVERVOLTAGE"] = True
    if cur is not None and abs(cur) > DEFAULT_LIMITS["current_max"]:
        alarms["OVERCURRENT"] = True
    return alarms


def _write_status(payload: dict) -> None:
    """Write status atomically: write to a temp file, then rename over the
    real path. rename() is atomic on the same filesystem, so a reader never
    sees a partially-written file."""
    try:
        with open(STATUS_TMP_PATH, "w") as f:
            json.dump(payload, f)
        os.replace(STATUS_TMP_PATH, STATUS_PATH)
    except Exception as e:
        logger.error(f"Failed to write status file: {e}")


def _poll_loop(sensor: INA228) -> None:
    """Single thread that owns all I2C access to the INA228."""
    logger.info("Poll loop started")
    prev_alarms: dict = {}

    while not _shutdown.is_set():
        try:
            reading = sensor.read()
            alarms = _check_alarms(reading)

            _write_status({
                "ok": True,
                "reading": reading,
                "alarms": alarms,
                "error": None,
                "timestamp": time.time(),
            })

            for alarm in alarms:
                if not prev_alarms.get(alarm):
                    logger.warning(f"Alarm raised: {alarm} (reading={reading})")
            prev_alarms = alarms

        except Exception as e:
            _write_status({
                "ok": False,
                "reading": {},
                "alarms": {},
                "error": str(e),
                "timestamp": time.time(),
            })
            logger.error(f"Read failed: {e}")

        _shutdown.wait(POLL_INTERVAL)
    logger.info("Poll loop stopped")


def main() -> None:
    def _on_signal(signum, frame):
        logger.info(f"Caught {signal.strsignal(signum)}, shutting down")
        _shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info("powerd starting")

    sensor = INA228(
        address=I2C_ADDRESS,
        shunt_resistance=SHUNT_RESISTANCE,
        max_current=MAX_CURRENT,
        i2c_bus=I2C_BUS,
    )
    sensor.open()
    sensor.configure()
    logger.info("INA228 open and configured")

    poll_thread = threading.Thread(target=_poll_loop, args=(sensor,), daemon=True)
    poll_thread.start()

    _shutdown.wait()

    logger.info("Closing INA228...")
    try:
        sensor.close()
    except Exception:
        pass

    # leave the last status file in place with an old timestamp - readers
    # already treat a stale timestamp as "daemon not running"
    logger.info("powerd stopped")


if __name__ == "__main__":
    main()