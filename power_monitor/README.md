# power_monitor

**Not implemented yet.** This folder contains draft code for a battery power-monitoring daemon. It is not part of the flight software and must not be deployed.

- **`powerd.py`**: draft standalone daemon meant to own an INA228 power monitor. Once per second it would read bus voltage, current, power, temperature and energy, check them against over/under-voltage and over-current limits, and write the result to `/tmp/powerd_status.json`.
- **`ina228.py`**: draft INA228 I2C driver used by `powerd.py`.

## Current state

- **Not installed:** `install_modules.py` does not install or enable `services/powerd.service`. Don't enable that unit by hand: the daemon isn't finished, and with `Restart=on-failure` it would restart in a loop.
- **Not read by telemd:** `POWER_CONTROLLER_ENABLED = False` in `telemetry/telemd.py`, so the telemetry has no power data. The ground station's "Power Controller" line shows `ERR` for this reason.

## Before enabling it

1. Finish `powerd.py` and test it against the real INA228.
2. Make the status-file path match: telemd reads `POWER_CONTROLLER_STATUS_PATH = "/tmp/power_cmd.json"`, while `powerd.py` writes `/tmp/powerd_status.json`.
3. Set `POWER_CONTROLLER_ENABLED = True` in `telemetry/telemd.py`.
4. Add `powerd.service` to the systemd part of `install_modules.py`: copy the unit, reload, enable, restart.

See the root [README.md](../README.md) for the overall architecture.
