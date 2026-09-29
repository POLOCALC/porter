# PORTER

PORTER is the flight/control software for the **POLOCALC** drone-based payload. It runs on the payload's onboard computer (a Raspberry Pi 5, `RPi5_dev` branch, porter version `4.0`) and:

- reads out the payload sensors (GPS, IMX5 IMU/INS, ADC, legacy inertial sensors, temperature) and writes their raw data to disk,
- drives the science camera (Alvium industrial camera through `pyalvium`, the compiled `alvium` binary, or a Sony camera through `sour_core`),
- configures a Valon RF synthesizer used as a signal source,
- drives a stabilized gimbal (through the `lager` module) for autonomous point-of-interest (POI) tracking,
- keeps a command and telemetry link with the ground station over an XBee radio.

The same entry point, `control.py`, is used for bench tests, integration and flight: only the YAML configuration file changes.

## How it fits together

Two processes run on the Pi, each as its own systemd service:

```
telemd.service  (always running, owns the XBee radio)
└── telemetry/telemd.py
      │  sudo systemctl start/stop porter@<config>
      ▼
porter@<config>.service  (started on command from the ground)
└── control.py -c config/<config>.yml
      ├── sensor threads ──► sensor binaries (ads1015, inertial, IMX5SensorModule, LM76SensorModule, alvium)
      ├── camera / gimbal threads
      ├── StatusWriter ──► /tmp/porter_status.json ──► read by telemd, sent to the ground as TEL
      └── CommandServer ◄── /tmp/porter_cmd.sock ◄── camera/gimbal commands relayed by telemd
```

- **telemd** starts at boot and never depends on `control.py`: the radio link, status and shell access keep working while porter is stopped, crashes or restarts.
- **porter** (`control.py`) runs in its own systemd unit, so restarting telemd never stops data taking, and stopping porter always stops every sensor binary it started.
- **Ground station:** `telemetry/remote_host.py` runs on the ground laptop, shows the telemetry and sends commands.

## Repository structure

```
porter/
├── control.py              # flight-software entry point (see "Running porter")
├── parameters.py           # constants: paths, timeouts, logging format, signals
├── exceptions.py           # ServiceExitError / FlagSetError used for clean shutdown
├── install_modules.py      # installer: venv, Python packages, sensor binaries, systemd units
├── config/
│   ├── default.yml         # configuration used by default (and overwritten by `setconfig`)
│   └── lm76.yaml           # example configuration with the LM76 temperature sensor enabled
├── porter/
│   ├── threads.py          # thread classes: Sensors, cameras, PointingController, StatusWriter
│   ├── process_utils.py    # start/stop of sensor binaries (process groups, registry, clean kill)
│   ├── valon.py            # serial driver for the Valon RF synthesizer
│   └── sensors/            # Python wrappers, one per sensor type (see "Sensors")
├── telemetry/              # radio daemon, ground station, radio protocol (see telemetry/README.md)
├── services/               # systemd units and boot scripts (see services/README.md)
├── power_monitor/          # draft INA228 power daemon, NOT implemented (see power_monitor/README.md)
├── decoders/               # offline scripts to decode/plot recorded data
├── modules/                # git submodules (sensor binaries, camera, gimbal code)
├── test/                   # manual hardware test scripts
├── setup_gps.py, startup_nmea.py, startup_script.sh   # old GPS helpers, not used (see "Legacy scripts")
├── version_history.txt     # changelog
└── LICENSE                 # MIT
```

## Requirements

- **Hardware:** Raspberry Pi 5 with the sensors on I2C/UART/USB, an XBee radio on USB (`/dev/ttyUSB0`), and optionally a Valon synthesizer, a Gremsy gimbal and an Alvium or Sony camera.
- **User and paths:** the software assumes user `polocalc`, home `/home/polocalc` (`parameters.home_directory`), the repository at **`/home/polocalc/flight/porter`**, and the Python virtual environment at **`/home/polocalc/porter_venv`**. The systemd units hard-code these paths.
- **Permissions:** `polocalc` needs passwordless `sudo` (telemd runs `sudo systemctl`, `sudo reboot`, `sudo shutdown`) and read/write access to the serial and I2C devices (on Raspberry Pi OS, membership of the `dialout` and `i2c` groups).
- **Build tools:** Rust (`cargo`) for the ADS1015, Inertial and LM76 binaries, and CMake/g++ for the Alvium binary.
- **Time sync:** `chrony` (telemd reports its status in the telemetry).

## Installation

1. **Clone with submodules** into the deployment path:
   ```sh
   git clone --recurse-submodules https://github.com/POLOCALC/porter /home/polocalc/flight/porter
   ```
   Some submodules use SSH URLs (see `.gitmodules`), so the Pi needs a GitHub SSH key.
2. **Run the installer** from the repository root, as `polocalc` (not with `sudo`; it calls `sudo` itself where needed):
   ```sh
   python3 install_modules.py
   ```
   It:
   - creates the venv at `/home/polocalc/porter_venv` if it doesn't exist,
   - installs the Python packages into the venv: `numpy`, `scipy`, `opencv-python-headless`, `vmbpy` (wheel in `modules/`), `pyalvium`, `lager` and `sour_core` (editable, from `modules/`), `digi-xbee`, `pyubx2`, `pyyaml`, `pyusb`, `adafruit-circuitpython-ina228`, `adafruit-circuitpython-mcp4725`,
   - builds the ADS1015, Inertial and LM76 binaries (`build_for_pi.sh` in each submodule, which also links them into `~/.local/bin`) and the Alvium binary (`build.sh` in Alvium-Camera-Module, which does not link it),
   - links the prebuilt `modules/IMX-5-Sensor-Module/bin/IMX5SensorModule` into `~/.local/bin`,
   - adds `~/.local/bin` to `PATH` in `~/.bashrc` if needed,
   - copies `services/telemd.service` and `services/porter@.service` to `/etc/systemd/system/`, reloads systemd, enables telemd and restarts it.

   If any step fails, the installer lists the failed steps, skips the systemd part and exits with code 1. Fix the problem and run it again: every step is safe to repeat.
3. **Optional:** install the CPU-governor unit by hand (see [services/README.md](services/README.md)).
4. **Check:** `systemctl status telemd` should be `active (running)`, and the ground station should start receiving telemetry.

The sensor binaries are called by name (`ads1015`, `inertial`, `IMX5SensorModule`, `LM76SensorModule`, `alvium`), so they must be on `PATH`. `porter@.service` starts `control.py` through `bash --login`, which loads `~/.profile` and with it `~/.local/bin` and the Vimba/GenICam variables. `build.sh` in Alvium-Camera-Module does not link the `alvium` binary; link it by hand if you use the `Alvium_Starspec` camera.

### Submodules

`modules/` contains git submodules, each developed in its own repository. Fix them there, not inside `porter`.

| Submodule | Purpose |
|---|---|
| `ADS1015-ADC-Module` | Rust program that configures and logs the ADS1015 ADC. |
| `Inertial-Sensors-Module` | Rust program for the legacy inertial sensors (IMU, magnetometer, barometer). |
| `LM76-Temperature-Sensor` | Rust program for the TI LM76 I2C temperature sensor. |
| `IMX-5-Sensor-Module` | C++ program (Inertial Sense SDK) for the IMX-5 IMU/INS; ships a prebuilt binary in `bin/`. |
| `Alvium-Camera-Module` | C/C++ program for the Alvium camera (`alvium` binary). |
| `Alvium-Camera-Module-Python` | Python module `pyalvium`: Alvium control through `vmbpy`, with parallel frame writing. |
| `lager` | "Live Attitude and Gimbal Error Resolver": drone and Gremsy T7 gimbal control for POI tracking. |
| `sour_core` | Shared code (e.g. Sony camera control) used by porter and the SOUR GUI project. |

## Running porter

### From the ground (normal operation)

In the ground station, `start` runs porter with `config/default.yml`. `start <name>` runs it with `config/<name>.yml`, for example `start test_configs/starspec`. `stop` stops it cleanly. See [telemetry/README.md](telemetry/README.md) for all commands.

### On the Pi, through systemd

The instance name after `@` selects the configuration file, relative to `config/` and without `.yml`:

```sh
sudo systemctl start porter@default                  # control.py -c config/default.yml
sudo systemctl start porter@test_configs-starspec    # control.py -c config/test_configs/starspec.yml
sudo systemctl stop 'porter@*'
journalctl -u 'porter@*' -f
```

In an instance name, `-` stands for `/`. To use a config whose file name contains a real `-`, get the unit name from `systemd-escape --template=porter@.service <name>`.

Only one porter instance should run at a time. telemd refuses `start` while any `porter@` unit is starting, running or stopping.

### By hand (development)

```sh
/home/polocalc/porter_venv/bin/python3 control.py [-c config/default.yml]
```

`-c` accepts a path relative to the repository root or an absolute path. Stop with Ctrl+C.

### What `control.py` does

1. **Creates the run folder** `/home/polocalc/data/NNN_YYYYMMDD_HHMMSS/`. `NNN` is the highest existing run number + 1, starting at `001`, so deleting old runs never reuses a number. The folder is linked as `/home/polocalc/data/current`.
2. **Saves a copy of the configuration** in the run folder and logs to `flight.log` there.
3. **Starts, in order:**
   1. the StatusWriter (1 Hz, `/tmp/porter_status.json`),
   2. one thread per configured sensor,
   3. the GNSS source for the gimbal (the first `GPS*` sensor),
   4. the Valon synthesizer (if a `source` block is configured),
   5. the camera,
   6. the pointing controller,
   7. the CommandServer (`/tmp/porter_cmd.sock`).
4. **Runs until SIGINT/SIGTERM,** or until a fatal error.

**Failure handling:**
- **A sensor that fails** to start or stops sending data is marked `dead` in the telemetry, with the reason. The other sensors keep running.
- **Any other error at startup** logs the traceback and shuts everything down with exit code 1. This includes a Valon that is configured but missing or not answering: a configured source is required.

**Shutdown:**
- It sets the shutdown flag, waits for all threads within one overall deadline (`SHUTDOWN_TIMEOUT` = 12 s), then stops any sensor binary still running (SIGTERM, then SIGKILL).
- A second signal during shutdown is logged and ignored, so the cleanup always finishes.
- The whole stop fits within the unit's `TimeoutStopSec=20`. After that, systemd kills anything left in the unit.

### Data layout

```
/home/polocalc/data/
├── current -> 007_20260928_120000/
└── 007_20260928_120000/
    ├── flight.log                            # porter log
    ├── default.yml                           # copy of the configuration used
    ├── sensors_data/
    │   ├── <sensor>_<timestamp>.bin          # data file (or folder, for Inertial) passed to each binary
    │   ├── <sensor>_<timestamp>_stdout.log   # stdout/stderr of that sensor's binary
    │   └── ...                               # binary-specific files (IMX5 *_imu.csv/_ins.csv/_inl2.csv, LM76 .csv, ...)
    └── camera_data/
```

`<sensor>` is the `name` from the configuration, and `<timestamp>` is the run's start time. When a sensor stops unexpectedly, its `_stdout.log` usually says why.

## Configuration

`control.py` reads one YAML file. The top-level keys are:

- **`global`**: `name`, `version`, `description`, and the flags `autostart_camera` (start the camera at startup, or wait for `camera.start`) and `autostart_poi_tracking`.
- **`sensors`**: one entry per sensor, with any key name (e.g. `GPS_1`, `ADC_1`). Each has:
  - `name`, used in file names and in the telemetry,
  - an optional `sensor_core`, the CPU core the binary is pinned to,
  - `connection`, with `type` (`serial` or `I2C`) and `parameters` (port/baud rate or bus/address),
  - `sensor_info`, whose `type` selects the driver, plus the manufacturer,
  - a driver-specific `configuration` block.

  A key starting with `GPS` is also used as the GNSS source for POI tracking.
- **`source`**: the Valon synthesizer: `port`, `baudrate`, `freq` (MHz, before `mult_factor`), `power` (dBm), `mod_amp` (dB) and `mod_freq` (Hz; 0 disables AM). If present, the Valon must answer, or porter aborts.
- **`camera`**: `name` (`Alvium`, `Alvium_Starspec` or `Sony`) plus camera settings (exposure, gain, format, frame rate, writing threads, ROI, ...).
- **`pointing_controller`**: `name: lager`, with a `gimbal` block (serial connection, mavlink settings) and a `poi` block (target latitude, longitude, altitude, `max_distance`).
- **`status_writer`**: present in the file but not read. The rate comes from `parameters.STATUS_WRITER_UPDATE_RATE`.

**Currently in `config/default.yml`:**
- **enabled:** ZED-F9P GPS, ADS1015 ADC, IMX5, MCP4725 DAC, legacy Inertial sensors, Alvium camera;
- **commented out:** LM76, Valon source, pointing controller.

**Changing the configuration remotely:** `setconfig <file>` from the ground station replaces `config/default.yml`. It checks the upload is valid YAML first and keeps the previous file as `config/default.yml.bak`. The new file is used at the next `start`.

`parameters.py` holds values that don't change per run:
- logging format and level,
- `ATTEMPTS` (retry count, e.g. Valon ID),
- the signals caught (`SIGINT`, `SIGTERM`),
- `SHUTDOWN_TIMEOUT`, `SENSOR_INIT_TIMEOUT`, `STATUS_WRITER_UPDATE_RATE`,
- the data-folder names and `INCREMENTAL_FILE_PREFIX` (the `NNN_` run numbers).

## Sensors

The driver is selected in `porter/sensors/sensors_handler.py` from `sensor_info.type`, ignoring case:

| `type` | Driver | How it works |
|---|---|---|
| `GPS` | `ubx.py` (`UBX`) | Python, `pyubx2` over serial. Detects the receiver's baud rate, switches it to the configured one, configures the UBX/NMEA messages, logs raw UBX to the `.bin` file (flushed every second), and reports fix and position in the telemetry. It stops after 10 s without data. |
| `ADC` | `ads1015.py` (`ADS1015`) | Runs the `ads1015` binary (I2C, configurable gain and data rate). |
| `Inertial` | `inertial.py` (`Inertial`) | Runs the `inertial` binary (I2C). The IMU output is disabled (`--no-imu`, hard-coded), so only the magnetometer and barometer record data. |
| `IMX5` | `IMX5SensorModule.py` | Runs the `IMX5SensorModule` binary (USB serial), with configurable IMU and INS rates. |
| `LM76` | `LM76SensorModule.py` | Runs the `LM76SensorModule` binary (I2C). `address` is required; optional alarm thresholds. |
| `DAC` | `mcp4725.py` (`MCP4725`) | Python, sets a fixed output voltage; it doesn't log data. |
| `inclinometer` | `KERNEL.py` | Inertial Labs KERNEL inclinometer. Supported in code, not used on this payload. |

**Wrappers that run a binary:**
- **Start:** the binary runs in its own process group, its output goes to `<sensor>_<timestamp>_stdout.log`, and it is registered in `porter/process_utils.py`, so shutdown can always stop it.
- **Health check:** every second the wrapper checks that the binary is alive and, after a 10 s grace period, that its output file was updated in the last 5 s. If not, the sensor is stopped and reported as `dead`.
- **Stop:** SIGTERM, then SIGKILL after 3 s.

Sensor health is tracked by `telemetry/StatusBoard.py`:
- **`ok`:** a heartbeat within the last 3 s.
- **`stale`:** 3–10 s since the last heartbeat.
- **`dead`:** more than 10 s, or the sensor never started. A sensor that failed to start or crashed also carries an `error` field with the reason.

## Camera, gimbal and source

- **Camera** (`camera` block, see `porter/threads.py`):
  - **`Alvium`** (Python, `pyalvium`) is started at startup or by `camera.start`.
  - **`Alvium_Starspec`** runs the `alvium` binary.
  - **`Sony`** uses `sour_core.sony`, in video or photo mode.

  telemd's `camera.capture` takes one frame with `telemetry/alvium_capture.py` and sends it to the ground.
- **Gimbal** (`pointing_controller` block): only `lager` is supported. It controls a Gremsy T7 over mavlink and can track the configured POI using the GNSS source. `gimbal.goto`, `gimbal.mode`, `gimbal.starttrack` and `gimbal.stoptrack` are available from the ground.
- **Valon synthesizer** (`source` block, `porter/valon.py`): at startup porter checks that the Valon answers its ID request, then sets the frequency, power and AM modulation. A configured Valon that is missing or silent stops the run.

## Decoders

`decoders/` contains standalone scripts, not used by `control.py`, to read and plot recorded data:
- `ubx.py`: GPS/UBX,
- `inertial.py`: legacy inertial sensors,
- `kernel.py`: KERNEL inclinometer,
- `ads1x15*.py`: ADC variants.

The `ads1x15*.py` decoders expect the old binary record format (`struct` records). The current `ads1015` binary writes text lines (`<timestamp> <value>`), so read new ADC files as text instead (e.g. `numpy.loadtxt`).

## Legacy scripts

`setup_gps.py`, `startup_nmea.py` and `startup_script.sh` predate the current design and are not used:
- `setup_gps.py` uses an old calling convention and does not run.
- `startup_nmea.py` assumes 38400 baud.
- `startup_script.sh` points to an old path.

The GPS is now configured by `porter/sensors/ubx.py` at every start. `test/` contains manual hardware test scripts (ADC timing, GPS), not automated tests.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| No telemetry on the ground | `journalctl -u telemd -f` on the Pi. telemd exits with an error, and systemd restarts it, if the XBee stops responding or one of its threads dies. |
| `start` fails or porter stops right away | `journalctl -u 'porter@*' -e`, then `flight.log` in `/home/polocalc/data/current/` |
| A sensor shows `dead` | its `_stdout.log` in `current/sensors_data/`, and the `error` field in the telemetry |
| Porter won't start: "already running" | `systemctl list-units 'porter@*'`, then `sudo systemctl stop 'porter@*'` |

## Version

See `version_history.txt`. Current version:

> **V4.0: Raspberry Pi 5 version of porter**
> - Alvium camera handled by a Python module with multithreaded writing
> - XBee modules for line-of-sight telemetry and control
> - `lager` module for gimbal control and drone telemetry
> - Autonomous POI tracking using the payload's GNSS or a drone's DRTK

## License

MIT License, Copyright (c) 2021 protocalc. See `LICENSE`.
