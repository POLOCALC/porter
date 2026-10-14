# PORTER

PORTER is the flight/control software for the **POLOCALC** drone-based payload. It runs on the payload's onboard computer (a Raspberry Pi 5, `RPi5_dev` branch, porter version `4.1`) and:

- reads out the payload sensors (GPS, IMX5 IMU/INS, ADC, legacy inertial sensors, temperature) and writes their raw data to disk,
- drives the science camera (Alvium industrial camera through `pyalvium`, the compiled `alvium` binary, or a Sony camera through `sour_core`),
- configures a Valon RF synthesizer used as a signal source,
- drives a stabilized gimbal (through the `lager` module) for autonomous point-of-interest (POI) tracking, using the drone's DRTK position or the payload GPS,
- keeps a command and telemetry link with the ground station over an XBee radio,
- stops the acquisition cleanly before the disk fills up.

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
      ├── camera thread
      ├── pointing controller (lager) ──► gimbal (MAVLink) + drone telemetry (passive) ──► POI tracking
      ├── DiskGuard ──► stops everything before the disk is full
      ├── StatusWriter ──► /tmp/porter_status.json ──► read by telemd, sent to the ground as TEL
      └── CommandServer ◄── /tmp/porter_cmd.sock ◄── camera/gimbal/position commands relayed by telemd
```

- **telemd** starts at boot and never depends on `control.py`. The radio link, the system status and shell access keep working while porter is stopped, crashes or restarts.
- **porter** (`control.py`) runs in its own systemd unit. Restarting telemd never stops data taking, and stopping porter always stops every sensor binary it started.
- **The ground station**, `telemetry/remote_host.py`, runs on the ground laptop: it shows the telemetry and sends commands. See [telemetry/README.md](telemetry/README.md).

## Repository structure

```
porter/
├── control.py                # flight-software entry point (see "Running porter")
├── parameters.py             # constants: paths, timeouts, logging format, signals
├── exceptions.py             # ServiceExitError / FlagSetError / DiskSpaceError
├── pi5_autoconfig.sh         # one-time Raspberry Pi 5 setup (network, sudo, packages, config.txt), then runs the installer
├── install_porter.sh         # installer: venv, Python packages, sensor binaries, systemd units (safe to re-run)
├── config.txt                # Raspberry Pi boot configuration, installed to /boot/firmware/config.txt
├── config/
│   └── default.yml           # configuration used by default (and replaced by `setconfig`)
├── porter/
│   ├── threads.py            # thread classes: Sensors, cameras, PointingController, StatusWriter, DiskGuard
│   ├── PositionSource.py     # chooses the position used for pointing: drone DRTK or payload GPS
│   ├── process_utils.py      # start/stop of sensor binaries (process groups, registry, clean kill)
│   ├── valon.py              # serial driver for the Valon RF synthesizer
│   └── sensors/              # Python wrappers, one per sensor type (see "Sensors")
├── telemetry/                # radio daemon, ground station, radio protocol (see telemetry/README.md)
├── services/                 # systemd units and boot scripts (see services/README.md)
├── decoder/
│   └── plot_sensors_data.py  # plots every sensor (and, through lager, the gimbal and drone) of a run
├── power_monitor/            # draft INA228 power daemon, NOT implemented (see power_monitor/README.md)
├── modules/                  # git submodules (sensor binaries, camera, gimbal and drone) + cross-build files
├── other/                    # old GPS helper scripts, not used (see "Legacy scripts")
├── test/                     # manual hardware test scripts
├── version_history.txt       # changelog
└── LICENSE                   # MIT
```

## Requirements

- **Hardware:** a Raspberry Pi 5 with:
  - the sensors on I2C/UART/USB,
  - an XBee radio on USB (`/dev/ttyUSB0`),
  - optionally a Valon synthesizer, a Gremsy T7 gimbal, a DJI drone telemetry link, and an Alvium or Sony camera.
- **Operating system:** Raspberry Pi OS Lite 64-bit (Bookworm), with SSH enabled.
- **User and paths:** the software assumes:
  - user `polocalc`, home `/home/polocalc` (`parameters.home_directory`),
  - the repository at **`/home/polocalc/flight/porter`**,
  - the Python virtual environment at **`/home/polocalc/porter_venv`**.

  The systemd units hard-code these paths.
- **Permissions:** `polocalc` needs:
  - passwordless `sudo`, because telemd runs `sudo systemctl`, `sudo reboot` and `sudo shutdown` (`pi5_autoconfig.sh` sets this up),
  - read/write access to the serial and I2C devices (the `dialout` and `i2c` groups).
- **Vimba X SDK** (Allied Vision) installed under `/opt/VimbaX_*`, for the Alvium camera (`vmbpy`, the `alvium` binary).
- **Time sync:** `chrony`. telemd reports its status in the telemetry.

The installer brings in the rest: the Rust toolchain if `cargo` is missing, `python3-venv`, the Python packages and the build of each submodule.

## Installation

### New Raspberry Pi: `pi5_autoconfig.sh`

1. **Flash** Raspberry Pi OS Lite 64-bit, enable SSH, create the user `polocalc`.
2. **Clone with submodules** into the deployment path:
   ```sh
   git clone --recurse-submodules https://github.com/POLOCALC/porter /home/polocalc/flight/porter
   ```
   Some submodules use SSH URLs (see `.gitmodules`), so the Pi needs a GitHub SSH key for those.
3. **Run the setup script** as `polocalc`:
   ```sh
   cd /home/polocalc/flight/porter
   bash pi5_autoconfig.sh
   ```
   It:
   1. sets a static IP (`192.168.4.57/24`),
   2. enables passwordless `sudo` and console auto-login for `polocalc`,
   3. disables unused services (Bluetooth, WPA supplicant, Avahi, cloud-init),
   4. installs the system packages (`git`, `cmake`, build tools, `libusb`, …) and builds `libopus`,
   5. installs this repository's [config.txt](config.txt) as `/boot/firmware/config.txt`, keeping the old one as `config.txt.bak`,
   6. updates `~/.bashrc`,
   7. restarts the network,
   8. runs **`install_porter.sh`** (see below),
   9. reboots.

   If the porter install fails, the script stops **before** rebooting, so the errors stay on screen.

### Install or update porter: `install_porter.sh`

After a `git pull`, or to repair an install, run the installer on its own, as `polocalc` (not with `sudo`; it calls `sudo` itself where needed):
```sh
cd /home/polocalc/flight/porter
bash install_porter.sh
```
It:
- **checks the prerequisites:**
  - initialises the git submodules,
  - installs `python3-venv` if needed,
  - installs the Rust toolchain (`rustup`, into `~/.cargo`) if `cargo` is missing,
- **sets up the venv:** creates `/home/polocalc/porter_venv` if it doesn't exist, and upgrades `pip`,
- **installs the Python packages into the venv:**
  - `numpy`, `scipy`, `opencv-python-headless`,
  - `vmbpy` (the wheel in `modules/`),
  - `pyalvium`, `lager` and `sour_core` (editable, from `modules/`),
  - `digi-xbee`, `pyubx2`, `pyyaml`, `pyusb`, `adafruit-circuitpython-ina228`, `adafruit-circuitpython-mcp4725`,
- **builds the binaries:**
  - ADS1015, Inertial and LM76: `build_for_pi.sh` in each submodule, which also links the binary into `~/.local/bin`,
  - Alvium: `build.sh` in Alvium-Camera-Module,
  - IMX5: links the prebuilt `modules/IMX-5-Sensor-Module/bin/IMX5SensorModule` into `~/.local/bin`,
- **installs the systemd units:** copies `services/telemd.service` and `services/porter@.service` to `/etc/systemd/system/`, reloads systemd, then enables and restarts telemd.

Every failed step is listed at the end. If any package or build fails, the systemd part is skipped and the script exits with code 1. Every step is safe to repeat. All output is also saved to `~/porter_install.log`.

**Check:** `systemctl status telemd` should be `active (running)`, and the ground station should start receiving telemetry.

**Binaries on `PATH`:** the sensor binaries are called by name (`ads1015`, `inertial`, `IMX5SensorModule`, `LM76SensorModule`, `alvium`). `porter@.service` starts `control.py` through `bash --login`, which loads `~/.profile`, and with it `~/.local/bin` and the Vimba/GenICam variables. Alvium's `build.sh` does not link the `alvium` binary into `~/.local/bin`: link it by hand if you use the `Alvium_Starspec` camera.

### Raspberry Pi boot configuration ([config.txt](config.txt))

Besides the Raspberry Pi OS defaults, it:
- enables UARTs 0–4, the I2C buses (including software buses 4 and 5 and `i2c3-pi5` on GPIO 6–7) and PPS on GPIO 18,
- disables Wi-Fi and Bluetooth (no radio emissions next to the radio telescopes),
- puts the USB-C port in **device mode** (`dtoverlay=dwc2,dr_mode=peripheral`), for the DJI E-Port connection of the Payload SDK. See [modules/lager/modules/Payload-SDK/README.md](modules/lager/modules/Payload-SDK/README.md).

### Cross-building the binaries (optional)

`modules/Dockerfile`, `modules/docker-compose.yml` and `modules/toolchain-rpi.cmake` set up a Debian Bookworm container for cross-compiling the sensor binaries for the Pi (aarch64) on a PC. The normal install builds on the Pi itself and doesn't need them.

### Submodules

`modules/` contains git submodules, each developed in its own repository. Fix them there, not inside `porter`, then commit the new submodule pointer in porter (`git add modules/<name>`).

| Submodule | Purpose |
|---|---|
| `ADS1015-ADC-Module` | Rust program that configures and logs the ADS1015 ADC. |
| `Inertial-Sensors-Module` | Rust program for the legacy inertial sensors (IMU, magnetometer, barometer). The IMU chip (ASM330LHH or MPU6050) is selected at run time. |
| `LM76-Temperature-Sensor` | Rust program for the TI LM76 I2C temperature sensor. |
| `IMX-5-Sensor-Module` | C++ program (Inertial Sense SDK) for the IMX-5 IMU/INS; ships a prebuilt binary in `bin/`. |
| `Alvium-Camera-Module` | C/C++ program for the Alvium camera (`alvium` binary). |
| `Alvium-Camera-Module-Python` | Python module `pyalvium`: Alvium control through `vmbpy`, with parallel frame writing. |
| `lager` | "Live Attitude and Gimbal Error Resolver": Gremsy T7 gimbal control and passive DJI drone telemetry, for POI tracking. It contains the `Payload-SDK` submodule, DJI's Payload SDK fork for the Matrice 400 telemetry through the E-Port (see its [README](modules/lager/modules/Payload-SDK/README.md)); `git submodule update --init --recursive` fetches it. |
| `sour_core` | Shared code (e.g. Sony camera control) used by porter and the SOUR GUI project. |

## Running porter

### From the ground (normal operation)

In the ground station:
- `start` runs porter with `config/default.yml`,
- `start <name>` runs it with `config/<name>.yml`, for example `start test_configs/starspec`,
- `stop` stops it cleanly.

See [telemetry/README.md](telemetry/README.md) for all commands.

### From the drone's remote controller (Matrice 400)

With `psdk-bridge.service` running, DJI Pilot 2 shows a POLOCALC widget panel. It travels over the drone's own radio link:
- **Start acquisition** runs porter with the configuration chosen in the **Configuration** list (the `.yml` files of `config/` when the service started; `default` is selected at start),
- **Stop acquisition** stops it,
- **POI tracking** starts or stops the gimbal's POI tracking,
- **Shutdown Pi** shuts the Pi down cleanly, like the XBee `shutdown` command: press it, then press it again within 5 s to confirm,
- the floating window shows a status line every 2 s, e.g. `RUN default_M400 12:34 | CAM 30fps | GPS 3D 24 | RTK 50 | BAT 78% | POI on 152m | 112GB | 50C`, and the reply to each command.

telemd runs these commands exactly like the XBee ones (same checks, one at a time), and the ground station sees them as `RC <command>: <reply>`. The XBee stays the only link while the drone is off, and on the Matrice 600.

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

`-c` accepts a path relative to the repository root, or an absolute path. Stop with Ctrl+C.

### What `control.py` does

1. **Creates the run folder** `/home/polocalc/data/NNN_YYYYMMDD_HHMMSS/`. `NNN` is the highest existing run number + 1, starting at `001`, so deleting old runs never reuses a number. The folder is linked as `/home/polocalc/data/current`.
2. **Saves a copy of the configuration** in the run folder, and logs to `flight.log` there.
3. **Starts, in order:**
   1. the StatusWriter (1 Hz, `/tmp/porter_status.json`),
   2. the disk-space check, then the DiskGuard (see "Disk space"),
   3. one thread per configured sensor,
   4. the GNSS source for the gimbal (the first `GPS*` sensor),
   5. the Valon synthesizer (if a `source` block is configured),
   6. the camera,
   7. the pointing controller,
   8. the CommandServer (`/tmp/porter_cmd.sock`).
4. **Runs until SIGINT/SIGTERM,** until the DiskGuard stops it, or until a fatal error.

**Failure handling:**
- **A sensor that fails** to start, or stops sending data, is marked `dead` in the telemetry with the reason. The other sensors keep running.
- **A gimbal or drone that is missing or disconnects** is reported in the telemetry (see "Pointing"), and porter keeps retrying. The run continues.
- **The camera:** an unknown camera name in the configuration stops the run. A camera that can't be opened, or stops while running, is logged in `flight.log`, but the sensors keep acquiring.
- **Other errors at startup** log the traceback and stop everything with exit code 1. That includes a Valon that is configured but missing or not answering: a configured source is required.

**Exit codes:** 0 = normal stop, 1 = error, 2 = stopped (or not started) because of disk space.

**Shutdown:**
- porter sets the shutdown flag, waits for all threads within one overall deadline (`SHUTDOWN_TIMEOUT` = 12 s), then stops any sensor binary still running (SIGTERM, then SIGKILL).
- A second signal during shutdown is logged and ignored, so the cleanup always finishes.
- The whole stop fits within the unit's `TimeoutStopSec=20`. After that, systemd kills anything left in the unit.

### Data layout

```
/home/polocalc/data/
├── current -> 007_20260928_120000/
└── 007_20260928_120000/
    ├── flight.log                            # porter log
    ├── default.yml                           # copy of the configuration used
    ├── uav_telemetry.bin                     # drone telemetry recorded by lager
    ├── gimbal_telemetry.bin                  # gimbal telemetry recorded by lager
    ├── sensors_data/
    │   ├── <sensor>_<timestamp>.bin          # data file (or folder, for Inertial) passed to each binary
    │   ├── <sensor>_<timestamp>_stdout.log   # stdout/stderr of that sensor's binary
    │   └── ...                               # binary-specific files (IMX5 *_imu.csv/_ins.csv/_inl2.csv, LM76 .csv, ...)
    ├── camera_data/
    ├── sensors_data_plots/                   # created by decoder/plot_sensors_data.py
    └── pointing_controller_plots/            # created by lager's plot_telemetry.py (run by the script above)
```

`<sensor>` is the `name` from the configuration, and `<timestamp>` is the run's start time. When a sensor stops unexpectedly, its `_stdout.log` usually says why.

## Configuration

`control.py` reads one YAML file. The top-level keys are:

- **`global`**:
  - `name`, `version`, `description`,
  - `autostart_camera` (start the camera at startup, or wait for `camera.start`),
  - `autostart_poi_tracking` (start POI tracking as soon as the gimbal is connected),
  - **`disk_guard`** (see "Disk space").
- **`sensors`**: one entry per sensor, with any key name (e.g. `GPS_1`, `ADC_1`). Each has:
  - `name`, used in file names and in the telemetry,
  - an optional `sensor_core`, the CPU core the binary is pinned to,
  - `connection`, with `type` (`serial` or `I2C`) and `parameters` (port/baud rate, or bus/address),
  - `sensor_info`, whose `type` selects the driver, plus the manufacturer,
  - a driver-specific `configuration` block.

  The first key starting with `GPS` is also the payload position source for pointing.
- **`source`**: the Valon synthesizer:
  - `port`, `baudrate`,
  - `freq` (MHz, before `mult_factor`), `power` (dBm),
  - `mod_amp` (dB) and `mod_freq` (Hz; 0 disables AM).

  If present, the Valon must answer, or porter aborts.
- **`camera`**: `name` (`Alvium`, `Alvium_Starspec` or `Sony`), plus camera settings (exposure, gain, format, frame rate, writing threads, ROI, …).
- **`pointing_controller`**: `name: lager`, with:
  - a `gimbal` block (Gremsy T7: serial port, baud rate, timeout, simulator flag),
  - a `uav` block (DJI drone telemetry: serial port, simulator flag),
  - a `poi` block (target latitude, longitude, **altitude above mean sea level**, `max_distance`).

  Leave out `gimbal` or `uav` if that device isn't used. Only configured devices appear in the telemetry.
- **`position_source`**: how the pointing position is chosen (see "Pointing").

**Changing the configuration remotely:** `setconfig <file>` from the ground station replaces `config/default.yml`. It checks the upload is valid YAML first, and keeps the previous file as `config/default.yml.bak`. The new file is used at the next `start`.

`parameters.py` holds values that don't change per run:
- logging format and level,
- `ATTEMPTS` (retry count, e.g. for the Valon ID),
- the signals caught (`SIGINT`, `SIGTERM`),
- `SHUTDOWN_TIMEOUT`, `SENSOR_INIT_TIMEOUT`, `STATUS_WRITER_UPDATE_RATE`,
- the data-folder names and `INCREMENTAL_FILE_PREFIX` (the `NNN_` run numbers).

## Sensors

The driver is selected in `porter/sensors/sensors_handler.py` from `sensor_info.type`, ignoring case:

| `type` | Driver | How it works |
|---|---|---|
| `GPS` | `ubx.py` (`UBX`) | Python, `pyubx2` over serial. It detects the receiver's baud rate, switches to the configured one, and configures the UBX/NMEA messages. Raw UBX goes to the `.bin` file, flushed every second. The telemetry reports the fix, the position, the number of satellites and the signal strength (top-4 C/N0). It stops after 10 s without data. |
| `ADC` | `ads1015.py` (`ADS1015`) | Runs the `ads1015` binary (I2C, configurable gain and data rate). |
| `Inertial` | `inertial.py` (`Inertial`) | Runs the `inertial` binary (I2C): magnetometer, barometer and, if `imu: True`, the IMU, whose chip is set with `imu_model: asm330lhh` or `mpu6050`. |
| `IMX5` | `IMX5SensorModule.py` | Runs the `IMX5SensorModule` binary (USB serial), with configurable IMU and INS rates. |
| `LM76` | `LM76SensorModule.py` | Runs the `LM76SensorModule` binary (I2C). `address` is required; the alarm thresholds are optional. |
| `DAC` | `mcp4725.py` (`MCP4725`) | Python, sets a fixed output voltage; it doesn't log data. |
| `inclinometer` | `KERNEL.py` | Inertial Labs KERNEL inclinometer. Supported in code, not used on this payload. |

**Wrappers that run a binary:**
- **Start:** the binary runs in its own process group, and its output goes to `<sensor>_<timestamp>_stdout.log`. It's registered in `porter/process_utils.py`, so shutdown can always stop it.
- **Health check:** every second the wrapper checks that the binary is alive. After a 10 s grace period, it also checks that the output file was updated in the last 5 s, and that it exists at all. If not, the sensor is stopped and reported as `dead`.
- **Stop:** SIGTERM, then SIGKILL after 3 s.

Health is tracked by `telemetry/StatusBoard.py`:
- **`ok`:** a heartbeat within the last 3 s.
- **`stale`:** 3–10 s since the last heartbeat.
- **`dead`:** more than 10 s, or never started. A sensor that failed to start or crashed also carries an `error` field with the reason.

Every configured sensor appears from the start, as `dead` until its first heartbeat, so a sensor that never starts can't silently disappear from the telemetry.

## Disk space

The data is on the same partition as the operating system, so a full disk would stop the Pi and lose data. Porter protects against that in two places:

- **Before starting,** both porter and telemd refuse to start a run with less than `start_min_free_gb` free.
- **During the run,** the DiskGuard thread checks the free space every `interval_s`. It measures the write rate over the last `window_s`, and estimates the time left before the free space reaches `min_free_gb`:

  | Time left | Level |
  |---|---|
  | more than `warn_margin_s` | `ok` |
  | less than `warn_margin_s` | `warning`, shown on the ground |
  | less than `stop_margin_s`, or below `min_free_gb` | `stopped`: clean stop of everything, exit code 2 |

```yaml
global:
  disk_guard:
    min_free_gb: 0.8          # never write below this
    start_min_free_gb: 5.0    # refuse to start below this
    stop_margin_s: 30         # stop when less than this much writing time is left
    warn_margin_s: 1200       # warn when less than this is left
    interval_s: 3             # check period
    window_s: 45              # window for the write-rate estimate
```
The ground station shows the level, the free space, the write rate and the estimated time left on the "Disk guard" line.

## Pointing (gimbal, drone and position source)

The `PointingController` thread (`porter/threads.py`) uses lager to:
- **connect to the Gremsy T7 gimbal** over MAVLink,
- **listen to the DJI drone's telemetry.** The drone link is **passive**: lager only receives packets, it never sends requests.

Each device's link is reported in the telemetry (`Gimbal` and `UAV` entries, and a header line on the ground). A device that isn't configured isn't reported.

| Device | Link states |
|---|---|
| Gimbal | `connecting`, `connected`, `not connected` (no heartbeat; retried every 10 s), `lost` (no message for more than 3 s) |
| UAV | `connecting`, `port error` (serial port missing; retried every 10 s), `waiting for data` (port open, no packet yet), `receiving`, `silent N s` |

Both links are opened in the background, so a missing device never blocks porter. POI tracking starts only when the gimbal is connected; a tracking request received earlier is kept.

**Position source.** The gimbal is pointed from the drone's DRTK position or from the payload GPS (`porter/PositionSource.py`):

| Mode | Behaviour |
|---|---|
| `auto` | Uses the DRTK while it's valid. Otherwise uses the payload GPS. Switching to the GPS is immediate; switching back to the DRTK waits `uav_hold_s`. |
| `uav` | DRTK only. If it isn't valid, no position: the gimbal holds still. |
| `payload` | Payload GPS only, likewise. |

- **The DRTK is valid** when the last drone packet is newer than `uav_max_age_s` and `rtk_pos_health` is in `uav_ok_health`. For example, 34 = float (dm accuracy) and 50 = fixed (cm accuracy).
- **The payload GPS is valid** when its last position is newer than `payload_max_age_s` and its fix is at least `payload_min_fix` (3 = 3D).

Both sources use the height above mean sea level, and so must the POI altitude.

```yaml
position_source:
  mode: auto                 # auto | payload | uav
  uav_max_age_s: 2.0
  uav_ok_health: [34, 48, 50]
  uav_hold_s: 10
  payload_max_age_s: 2.0
  payload_min_fix: 3
```
The mode can be changed during a flight with `position.mode <auto|payload|uav>`. The current source and the reason for it appear in the `Position` entry of the telemetry. Every switch is logged in `flight.log`.

**Gimbal commands** from the ground (`gimbal.goto`, `gimbal.mode`, `gimbal.starttrack`, `gimbal.stoptrack`) are refused with `gimbal not connected` while the gimbal isn't connected. `gimbal.mode` replies at once, and the change finishes in the background: the `mode` field in the telemetry shows when it's done.

## Camera and source

- **Camera** (`camera` block, see `porter/threads.py`):
  - **`Alvium`** (Python, `pyalvium`) starts at startup or with `camera.start`.
  - **`Alvium_Starspec`** runs the `alvium` binary.
  - **`Sony`** uses `sour_core.sony`, in video or photo mode.

  telemd's `camera.capture` takes one frame with `telemetry/alvium_capture.py` and sends it to the ground.
- **Valon synthesizer** (`source` block, `porter/valon.py`): at startup, porter checks that the Valon answers its ID request, then sets the frequency, power and AM modulation. A configured Valon that is missing or silent stops the run.

## Plotting a run

[decoder/plot_sensors_data.py](decoder/plot_sensors_data.py) plots every sensor of a run folder, using the configuration copy saved there to find the files and settings:
```sh
/home/polocalc/porter_venv/bin/python3 decoder/plot_sensors_data.py [run_folder] [--dpi 120] [--no-telemetry]
```
- **Run folder:** defaults to `/home/polocalc/data/current`.
- **Plots** are saved as PNG in `<run>/sensors_data_plots/`; nothing is shown on screen:
  - one figure per quantity, with one row per axis for three-axis data,
  - a sample-timing figure for each stream (intervals, histogram, rate, largest gap).
- **Time axis:** all plots share one axis (seconds since the run start). The GPS time of week is converted to it.
- **The pointing controller:** if the configuration has one, the script also runs lager's `decoder/plot_telemetry.py` on the same folder, for the drone and gimbal plots in `<run>/pointing_controller_plots/`. `--no-telemetry` skips that.

## Legacy scripts

`other/` holds `setup_gps.py`, `startup_nmea.py` and `startup_script.sh`. They predate the current design and are not used:
- `setup_gps.py` uses an old calling convention and doesn't run,
- `startup_nmea.py` assumes 38400 baud,
- `startup_script.sh` points to an old path.

The GPS is now configured by `porter/sensors/ubx.py` at every start. `test/` contains manual hardware test scripts (ADC timing, GPS), not automated tests.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| No telemetry on the ground | `journalctl -u telemd -f` on the Pi. telemd exits with an error, and systemd restarts it, if the XBee stops responding or one of its threads dies. |
| `start` fails, or porter stops right away | `journalctl -u 'porter@*' -e`, then `flight.log` in `/home/polocalc/data/current/`. Exit code 2 means not enough disk space. |
| A sensor shows `dead` | its `_stdout.log` in `current/sensors_data/`, and the `error` field in the telemetry |
| `Gimbal: NOT CONNECTED` | gimbal power and serial cable; `flight.log` ("Gimbal not connected: ...") |
| `UAV: WAITING FOR DATA` / `SILENT` | the drone is off, its telemetry output is disabled, or the cable is unplugged. The link is passive, so porter can only wait for packets. |
| Gimbal points from the payload GPS instead of the DRTK | the `Position` entry's `reason` (e.g. `UAV RTK health 16`), and `uav_ok_health` |
| Porter won't start: "already running" | `systemctl list-units 'porter@*'`, then `sudo systemctl stop 'porter@*'` |
| The install failed | `~/porter_install.log` |

## Version

See `version_history.txt`. Current version:

> **V4.1**
> - Several bugs fixed
> - Tested Valon and other RF-related components
> - New thermal control handling for the frequency multiplier
> - DiskGuard thread for a safe acquisition stop when the disk is full

## License

MIT License, Copyright (c) 2021 protocalc. See `LICENSE`.
