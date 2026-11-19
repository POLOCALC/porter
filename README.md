# PORTER

PORTER is the flight/control software for the **POLOCALC** drone-based payload. It runs on the payload's onboard computer (a Raspberry Pi 5, `main` branch, porter version `4.2`) and:

- reads out the payload sensors (GPS, IMX5 IMU/INS, ADC, legacy inertial sensors, temperature) and writes their raw data to disk,
- drives the science camera (Alvium industrial camera over USB through `pyalvium` or over the Pi 5 CSI-2 connector through `pyalvium_csi`, the compiled `alvium` binary, or a Sony camera through `sour_core`),
- configures the RF synthesizer used as a signal source (Valon or Windfreak SynthHD),
- drives a stabilized gimbal (through the `lager` module) for autonomous point-of-interest (POI) tracking, using the drone's DRTK position or the payload GPS,
- keeps a command and telemetry link with the ground station over an XBee radio,
- stops the acquisition cleanly before the disk fills up.

The same entry point, `control.py`, is used for bench tests, integration and flight: only the YAML configuration file changes.

## How it fits together

porter runs in its own systemd service, started and stopped by one or both links, each its own service too:

```
telemd.service  (while the XBee is plugged in)     rcd.service  (Matrice 400, with psdk-bridge)
└── telemetry/telemd.py                            └── telemetry/rcd.py ◄── /tmp/psdk_control.sock ◄── DJI Pilot 2 widgets
      │                                                  │
      └──── payload_control.py: sudo systemctl start/stop porter@<config>, one at a time (file lock)
      ▼
porter@<config>.service  (started on command from the ground or the RC)
└── control.py -c config/<config>.yml
      ├── sensor threads ──► sensor binaries (ads1015, inertial, IMX5SensorModule, LM76SensorModule, alvium)
      ├── camera thread
      ├── pointing controller (lager) ──► gimbal (MAVLink) + drone telemetry (passive) ──► POI tracking
      ├── DiskGuard ──► stops everything before the disk is full
      ├── StatusWriter ──► /tmp/porter_status.json ──► read by telemd, sent to the ground as TEL
      └── CommandServer ◄── /tmp/porter_cmd.sock ◄── camera/gimbal/position commands relayed by telemd
```

- **telemd** runs whenever the XBee is plugged in (also at boot) and never depends on `control.py`. The radio link, the system status and shell access keep working while porter is stopped, crashes or restarts. Without the XBee it doesn't run at all.
- **rcd** (Matrice 400) connects to psdk-bridge and runs the DJI Pilot 2 widget commands. It doesn't need telemd or the XBee: with the XBee removed, the RC is the only link.
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
├── requirements.lock         # exact Python package versions installed by install_porter.sh
├── config.txt                # Raspberry Pi boot configuration, installed to /boot/firmware/config.txt
├── config/                   # YAML configurations (see "Configuration files")
├── porter/
│   ├── threads.py            # thread classes: Sensors, cameras, PointingController, StatusWriter, DiskGuard
│   ├── PositionSource.py     # chooses the position used for pointing: drone DRTK or payload GPS
│   ├── process_utils.py      # start/stop of sensor binaries (process groups, registry, clean kill)
│   ├── valon.py              # serial driver for the Valon RF synthesizer
│   ├── windfreak/            # serial driver for the Windfreak SynthHD RF synthesizer
│   └── sensors/              # Python wrappers, one per sensor type (see "Sensors")
├── telemetry/                # radio daemon, ground station, radio protocol (see telemetry/README.md)
├── services/                 # systemd units and boot scripts (see services/README.md)
├── decoder/
│   └── plot_sensors_data.py  # plots every sensor (and, through lager, the gimbal and drone) of a run
├── power_monitor/            # draft INA228 power daemon, NOT implemented (see power_monitor/README.md)
├── modules/                  # git submodules (sensor binaries, camera, gimbal and drone) + cross-build files
├── other/                    # camera_preview.py (focus/exposure tool) and old GPS scripts (see "Other scripts")
├── test/                     # manual hardware test scripts
├── version_history.txt       # changelog
└── LICENSE                   # MIT
```

## Requirements

- **Hardware:** a Raspberry Pi 5 with:
  - the sensors on I2C/UART/USB,
  - an XBee radio on USB (`/dev/ttyUSB0`),
  - optionally a Valon or Windfreak SynthHD synthesizer, a Gremsy T7 gimbal, a DJI drone telemetry link, and an Alvium (USB or CSI-2) or Sony camera.
- **Operating system:** Raspberry Pi OS Lite 64-bit (Bookworm), with SSH enabled.
- **User and paths:** the software assumes:
  - user `polocalc`, home `/home/polocalc` (`parameters.home_directory`),
  - the repository at **`/home/polocalc/flight/porter`**,
  - the Python virtual environment at **`/home/polocalc/porter_venv`**.

  The systemd units hard-code these paths.
- **Permissions:** `polocalc` needs:
  - passwordless `sudo`, because telemd runs `sudo systemctl`, `sudo reboot` and `sudo shutdown` (`pi5_autoconfig.sh` sets this up),
  - read/write access to the serial and I2C devices (the `dialout` and `i2c` groups).
- **Vimba X SDK** (Allied Vision), for the Alvium camera (`vmbpy`, the `alvium` binary). `install_porter.sh` unpacks `modules/VimbaX_Setup-*-Linux_ARM64.tar.gz` into `/opt/VimbaX_<version>` and runs `cti/Install_GenTL_Path.sh` (sets `GENICAM_GENTL64_PATH` from `/etc/profile.d`, active after a reboot). The tarball is **not in the repository** (Allied Vision licence, 74 MB; ignored by `.gitignore`): download it from the [Vimba X page](https://www.alliedvision.com/en/products/vimba-sdk/) (ARM64 version, after accepting the licence) and copy it into `modules/` on the Pi before running the installer. Without it, the installer skips Vimba X with a warning (listed at the end as "Skipped") and installs everything else: the USB Alvium won't work until you copy the tarball and run the installer again. The CSI Alvium doesn't need Vimba X.
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
  - every package at the exact version listed in `requirements.lock` (taken from the working flight Pi, so new PyPI releases can't break an install),
  - `vmbpy` (the wheel in `modules/`),
  - `pyalvium`, `pyalvium_csi`, `lager` and `sour_core` (editable, from `modules/`, without dependencies: those are in `requirements.lock`),
  - then runs `pip check`, which fails if `requirements.lock` misses a dependency,
- **builds the binaries:**
  - ADS1015, Inertial and LM76: `build_for_pi.sh` in each submodule, which also links the binary into `~/.local/bin`,
  - the Alvium CSI-2 kernel driver: `make && sudo make install` in `Alvium-Camera-Module-CSI/alvium-raspberrypi-driver-release`,
  - Vimba X: unpacks the SDK into `/opt` (see "Requirements") and points the `vimbax` alias in `~/.bashrc` to its viewer; skipped with a warning if the tarball isn't in `modules/`,
  - the `alvium` binary (Alvium-Camera-Module, for `Alvium_Starspec`) is **not** built any more: its line is commented out in the installer,
  - IMX5: links the prebuilt `modules/IMX-5-Sensor-Module/bin/IMX5SensorModule` into `~/.local/bin`,
- **installs the systemd units:** copies `services/telemd.service`, `porter@.service`, `psdk-bridge.service` and `rcd.service` to `/etc/systemd/system/` and `services/99-xbee.rules` to `/etc/udev/rules.d/`, reloads systemd, then enables telemd (and psdk-bridge and rcd if `psdk_telemetry` was built). psdk-bridge and rcd are not started by the installer, because connecting to the drone takes about a minute: they start at the next reboot. Set the XBee's USB serial in `99-xbee.rules` first, see [services/README.md](services/README.md).

Every failed step is listed at the end. If any package or build fails, the systemd part is skipped and the script exits with code 1. A missing Vimba X tarball is not a failure: it's listed separately as "Skipped". Every step is safe to repeat. All output is also saved to `~/porter_install.log`.

**Check:** with the XBee plugged in, `systemctl status telemd` should be `active (running)`, and the ground station should start receiving telemetry. On a Matrice 400, `systemctl status rcd` should be `active (running)`, and DJI Pilot 2 should show the status line.

**Binaries on `PATH`:** the sensor binaries are called by name (`ads1015`, `inertial`, `IMX5SensorModule`, `LM76SensorModule`, `alvium`). `porter@.service` starts `control.py` through `bash --login`, which loads `~/.profile`, and with it `~/.local/bin` and the Vimba/GenICam variables. The installer doesn't build the `alvium` binary: to use the `Alvium_Starspec` camera, run `build.sh` in `modules/Alvium-Camera-Module` and link the binary into `~/.local/bin` by hand.

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
| `Alvium-Camera-Module-Python` | Python module `pyalvium`: Alvium control through `vmbpy` (USB), with parallel frame writing. |
| `Alvium-Camera-Module-CSI` | Python module `pyalvium_csi`: same interface as `pyalvium`, for an Alvium on the Pi 5 CSI-2 connector (`media-ctl`/`v4l2-ctl`). Also holds Allied Vision's CSI-2 kernel driver. |
| `lager` | "Live Attitude and Gimbal Error Resolver": Gremsy T7 gimbal control and passive DJI drone telemetry (Matrice 600 over serial, Matrice 400 through the Payload SDK), for POI tracking with an optional constant roll. It contains the `Payload-SDK` submodule, DJI's Payload SDK fork for the Matrice 400 telemetry through the E-Port (see its [README](modules/lager/modules/Payload-SDK/README.md)); `git submodule update --init --recursive` fetches it. |
| `sour_core` | Shared code (e.g. Sony camera control) used by porter and the SOUR GUI project. |

## Running porter

### From the ground (normal operation)

In the ground station:
- `start <name>` runs porter with `config/<name>.yml`, for example `start 90GHz_M400`,
- `start` runs it with `config/default.yml` (in the repository, a copy of `90GHz_M400.yml`; replace it on the Pi, or with `setconfig`, to change what a plain `start` uses),
- `stop` stops it cleanly.

See [telemetry/README.md](telemetry/README.md) for all commands.

### From the drone's remote controller (Matrice 400)

With `psdk-bridge.service` and `rcd.service` running, DJI Pilot 2 shows a POLOCALC widget panel. It travels over the drone's own radio link:
- **Start acquisition** runs porter with the configuration chosen in the **Configuration** list (the `.yml` files of `config/` when the service started; `default` is selected at start if it exists, otherwise the first file in alphabetical order),
- **Stop acquisition** stops it,
- **POI tracking** starts or stops the gimbal's POI tracking,
- **Shutdown Pi** shuts the Pi down cleanly, like the XBee `shutdown` command: press it, then press it again within 5 s to confirm,
- the floating window shows a status line every 2 s, e.g. `RUN 90GHz_M400 12:34 | CAM 30fps | GPS 3D 24 | RTK 50 | BAT 78% | POI on 152m | PPS | 112GB | 50C` (`PPS`, the chrony reference, or `NOSYNC`), and the reply to each command.

rcd runs these commands with the same checks as the XBee ones (`telemetry/payload_control.py`), and the two links never start or stop porter at the same time. RC commands appear in `journalctl -u rcd`, not on the ground station. The XBee stays the only link while the drone is off, and on the Matrice 600.

### On the Pi, through systemd

The instance name after `@` selects the configuration file, relative to `config/` and without `.yml`:

```sh
sudo systemctl start porter@90GHz_M400               # control.py -c config/90GHz_M400.yml
sudo systemctl start porter@test_configs-starspec    # control.py -c config/test_configs/starspec.yml
sudo systemctl stop 'porter@*'
journalctl -u 'porter@*' -f
```

In an instance name, `-` stands for `/`. To use a config whose file name contains a real `-`, get the unit name from `systemd-escape --template=porter@.service <name>`. `porter@default` uses `config/default.yml`.

Only one porter instance should run at a time. telemd and rcd refuse `start` while any `porter@` unit is starting or running, and wait for a stop in progress to finish.

### By hand (development)

```sh
/home/polocalc/porter_venv/bin/python3 control.py -c config/90GHz_M400.yml
```

`-c` accepts a path relative to the repository root, or an absolute path; without `-c`, `control.py` uses `config/default.yml`. Stop with Ctrl+C.

### What `control.py` does

1. **Creates the run folder** `/home/polocalc/data/NNN_YYYYMMDD_HHMMSS/`. `NNN` is the highest existing run number + 1, starting at `001`, so deleting old runs never reuses a number. The folder is linked as `/home/polocalc/data/current`.
2. **Saves a copy of the configuration** in the run folder, and logs to `flight.log` there.
3. **Starts, in order:**
   1. the StatusWriter (1 Hz, `/tmp/porter_status.json`),
   2. the disk-space check, then the DiskGuard (see "Disk space"),
   3. one thread per configured sensor,
   4. the GNSS source for the gimbal (the first `GPS*` sensor),
   5. the RF synthesizer, Valon or SynthHD (if a `source` block is configured),
   6. the camera,
   7. the pointing controller,
   8. the CommandServer (`/tmp/porter_cmd.sock`).
4. **Runs until SIGINT/SIGTERM,** until the DiskGuard stops it, or until a fatal error.

**Failure handling:**
- **A sensor that fails** to start, or stops sending data, is marked `dead` in the telemetry with the reason. The other sensors keep running.
- **A gimbal or drone that is missing or disconnects** is reported in the telemetry (see "Pointing"), and porter keeps retrying. The run continues.
- **The camera:** an unknown camera name in the configuration stops the run. A camera that can't be opened, or stops while running, is logged in `flight.log`, but the sensors keep acquiring.
- **Other errors at startup** log the traceback and stop everything with exit code 1. That includes a synthesizer (Valon or SynthHD) that is configured but missing or not answering: a configured source is required.

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
    ├── <config>.yml                          # copy of the configuration used
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

### Configuration files

`config/` holds:

| File | Use |
|---|---|
| `<band>_<drone>.yml`: `90GHz_*`, `150GHz_*`, `260GHz_*` | Flight configurations, one per source band and drone setup. The band sets the `source` block and the camera: Alvium (USB) at 90 GHz, Alvium CSI-2 at 150 GHz, Sony at 260 GHz. |
| … `_M400` | DJI Matrice 400 with the Gremsy T7: gimbal control and drone telemetry through psdk-bridge. |
| … `_M600_gremsy` | DJI Matrice 600 with the Gremsy T7: gimbal control and drone telemetry over serial. |
| … `_M600_ronin` | DJI Matrice 600 with the Ronin gimbal: no `pointing_controller` block, so porter drives neither the gimbal nor reads the drone telemetry. |
| `example_M400.yml`, `example_M600_gremsy.yml`, `example_M600_ronin.yml`, `example_AlviumCSI.yml` | Starting points for new configurations, without a `source` block. |
| `test_synth_config.yml` | Bench test of the SynthHD alone. |

`config/default.yml` is the file used by a plain `start`, by `control.py` without `-c`, the one the RC preselects, and the one `setconfig` overwrites. In the repository it's a copy of `90GHz_M400.yml`. A `setconfig` changes it on the Pi, so `git status` there shows it as modified: run `git checkout config/default.yml` before a `git pull` if git refuses to update it.

### Keys

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
- **`source`**: the RF synthesizer:
  - `name`: `valon` or `synthhd` (any case),
  - a `config` block with:
    - `port` (and `baudrate` for the Valon),
    - `freq`: the **output frequency in GHz, after the multiplier**, and `mult_factor`: porter sets the synthesizer to `freq / mult_factor`,
    - `power` (dBm),
    - `mod_amp` (dB) and `mod_freq` (Hz; 0 disables AM),
    - SynthHD only: `channel` (RF output, 0 or 1) and `mod_funct` (`sine` or `square_wave`). To drive both channels, give `channel`, `freq`, `mult_factor`, `power`, `mod_amp`, `mod_freq` and `mod_funct` as lists, one value per channel.

  ```yaml
  source:
    name: valon
    config:
      port: /dev/ttyAMA3
      baudrate: 115200
      freq: 93            # GHz, after the multiplier
      mult_factor: 6
      power: -10          # dBm
      mod_amp: 22         # dB
      mod_freq: 37        # Hz
  ```

  If present, the synthesizer must answer, or porter aborts.
- **`camera`**: `name` (`Alvium`, `Alvium_CSI`, `Alvium_Starspec` or `Sony`), plus camera settings (exposure, gain, format, frame rate, writing threads, ROI, …).
- **`pointing_controller`**: `name: lager`, with:
  - a `gimbal` block (Gremsy T7: serial port, baud rate, timeout, simulator flag),
  - a `uav` block (DJI drone telemetry: the serial port on the Matrice 600; on the Matrice 400, `socket: /tmp/psdk_telemetry.sock` to read it from psdk-bridge),
  - a `poi` block (target latitude, longitude, **altitude above mean sea level**, `max_distance`, and an optional `roll_deg`: a constant gimbal roll kept while tracking, default 0 = horizon level).

  Leave out `gimbal` or `uav` if that device isn't used. Only configured devices appear in the telemetry.
- **`position_source`**: how the pointing position is chosen (see "Pointing").

**Changing the configuration remotely:** `setconfig <file>` from the ground station overwrites `config/default.yml`. It checks the upload is valid YAML first, and keeps the previous file as `config/default.yml.bak`. The new file is used at the next plain `start`.

**Duplicate keys:** if a top-level key such as `pointing_controller` appears twice in a file, YAML silently keeps only the last one. Keep one block per key.

`parameters.py` holds values that don't change per run:
- logging format and level,
- `ATTEMPTS` (retry count, e.g. for the synthesizer ID),
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
  - **`Alvium`** (Python, `pyalvium`, USB) starts at startup or with `camera.start`.
  - **`Alvium_CSI`** (Python, `pyalvium_csi`, Pi 5 CSI-2 connector) works the same way. It needs the CSI-2 kernel driver built by the installer.
  - **`Alvium_Starspec`** runs the `alvium` binary.
  - **`Sony`** uses `sour_core.sony`, in video or photo mode.

  telemd's `camera.capture` takes one frame with `telemetry/alvium_capture.py` and sends it to the ground.
- **RF synthesizer** (`source` block): **Valon** (`porter/valon.py`) or **Windfreak SynthHD** (`porter/windfreak/`). At startup, porter checks that the synthesizer answers its ID request, then sets the frequency, power and AM modulation (SynthHD: sine or square wave, through its AM lookup table). A configured synthesizer that is missing or silent stops the run.

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

## Other scripts

**`other/camera_preview.py`:** live view of the Alvium camera, for setting exposure, gain and focus before a flight. It needs a display (HDMI, VNC or `ssh -X`), and porter must not be acquiring at the same time:
```sh
/home/polocalc/porter_venv/bin/python3 other/camera_preview.py --backend csi    # or --backend usb
```
It shows a focus value (to maximise while turning the focus ring), can save frames, and prints the settings as a YAML snippet for the configuration. The full key list is at the top of the script.

**Legacy scripts:** `other/` also holds `setup_gps.py`, `startup_nmea.py` and `startup_script.sh`. They predate the current design and are not used:
- `setup_gps.py` uses an old calling convention and doesn't run,
- `startup_nmea.py` assumes 38400 baud,
- `startup_script.sh` points to an old path.

The GPS is now configured by `porter/sensors/ubx.py` at every start. `test/` contains manual hardware test scripts (ADC timing, GPS), not automated tests.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| No telemetry on the ground | `ls -l /dev/xbee` (no link: the XBee isn't seen, or the serial in `99-xbee.rules` is wrong), then `journalctl -u telemd -f`. telemd exits with an error, and systemd restarts it, if the XBee stops responding or one of its threads dies. |
| DJI Pilot 2 shows "waiting for telemd" | rcd isn't connected: `systemctl status rcd`, `journalctl -u rcd -f` (the message text is psdk_telemetry's, it means rcd). |
| `start` fails, or porter stops right away | `journalctl -u 'porter@*' -e`, then `flight.log` in `/home/polocalc/data/current/`. Exit code 2 means not enough disk space. |
| A sensor shows `dead` | its `_stdout.log` in `current/sensors_data/`, and the `error` field in the telemetry |
| `Gimbal: NOT CONNECTED` | gimbal power and serial cable; `flight.log` ("Gimbal not connected: ...") |
| `UAV: WAITING FOR DATA` / `SILENT` | the drone is off, its telemetry output is disabled, or the cable is unplugged. The link is passive, so porter can only wait for packets. |
| Gimbal points from the payload GPS instead of the DRTK | the `Position` entry's `reason` (e.g. `UAV RTK health 16`), and `uav_ok_health` |
| Porter won't start: "already running" | `systemctl list-units 'porter@*'`, then `sudo systemctl stop 'porter@*'` |
| The install failed | `~/porter_install.log` |

## Version

See `version_history.txt`. Current version:

> **V4.2.1**
> - New systemd service for the DJI Matrice 400 PSDK telemetry
>
> **V4.2**
> - Support for the DJI Matrice 400
> - Bugs fixed

## License

MIT License, Copyright (c) 2021 protocalc. See `LICENSE`.