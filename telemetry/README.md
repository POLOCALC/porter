# telemetry

The ground link of PORTER: the daemon that owns the payload's XBee radio (`telemd.py`), the ground-station program (`remote_host.py`), and the pieces they share.

| File | Runs on | Role |
|---|---|---|
| `telemd.py` | Pi, as `telemd.service` | Owns the XBee radio: sends telemetry, executes commands from the ground, starts and stops porter. |
| `remote_host.py` | ground laptop | Terminal UI: shows the telemetry and sends commands. |
| `Xbee.py` | both | XBee wrapper (`digi-xbee`): opening, message chunking, end-of-message marker, radio IDs. |
| `command_server.py` | Pi, inside `control.py` | Unix socket `/tmp/porter_cmd.sock` that relays camera/gimbal commands from telemd to porter's threads. |
| `StatusBoard.py` | Pi, inside `control.py` | Thread-safe sensor health board (heartbeats, `ok`/`stale`/`dead`, error reasons). |
| `alvium_capture.py` | Pi, run by telemd | Captures one Alvium frame for `camera.capture`. |
| `default.yml` | — | Not read by any code (`control.py` uses `config/default.yml`). Kept as an example: it has the LM76 sensor enabled. |

## telemd

### Running

`telemd.py` runs as a systemd service (see [services/README.md](../services/README.md)):

```sh
systemctl status telemd
journalctl -u telemd -f
sudo systemctl restart telemd
```

Settings are constants at the top of `telemd.py`:

| Setting | Value |
|---|---|
| XBee port and baud rate | `/dev/ttyUSB0`, 38400 |
| Ground radio | `REMOTE_NAME = "OBI"`, a key in `Xbee.IDs` |
| Status file (from porter) | `/tmp/porter_status.json` |
| Command socket (to porter) | `/tmp/porter_cmd.sock` |
| Configuration replaced by `setconfig` | `config/default.yml` |
| Log sent by `getlog` | `/home/polocalc/data/current/flight.log` |

### Threads

telemd runs five threads. Only the I/O thread touches the radio.

| Thread | Job |
|---|---|
| `io` | Sends queued replies, then the newest telemetry packet, then reads incoming frames and handles complete commands. |
| `telemetry` | Once per second, builds the TEL packet (see below) and puts it in a single slot, replacing the previous one. |
| `worker` | Runs slow commands one at a time (`start`, `stop`, `camera.capture`), so the radio thread never blocks. |
| `jobs` | Watches background shell jobs and sends their output when they finish. |
| `chrony` | Every 5 s, asks `chronyc tracking` whether time is synced and to which reference. |

**Self-recovery:**
- If the XBee returns 25 errors in a row (about 5 s, e.g. the USB device disappeared), telemd exits with code 1.
- The main thread checks every 2 s that all five threads are alive. If one died, telemd exits with code 1.
- In both cases `Restart=on-failure` restarts it after 5 s.

## Commands

Commands are typed in the ground station. Replies start with `ACK:` or `ERR:` unless stated otherwise. Commands are case-insensitive, except for config names, paths and shell commands, which keep their case.

| Command | What it does | Reply |
|---|---|---|
| `ping` | Link check. | `pong - uplink RSSI: <dBm> dBm` |
| `start` | Starts porter with `config/default.yml` (`porter@default.service`). | `ACK:start queued`, then `ACK:porter_start <unit>` or `ERR:...` |
| `start <name>` | Starts porter with `config/<name>.yml`, e.g. `start test_configs/starspec`. The name may contain letters, digits, `_`, `-` and `/`. The file must exist. | as above |
| `stop` | Stops porter cleanly (`systemctl stop 'porter@*'`, up to 20 s). | `ACK:stop queued`, then `ACK:porter_stop` |
| `getlog` | Sends the last 40 KiB of `flight.log`. | `LOGDATA:<base64>`, saved as `flight.log` on the ground |
| `setconfig <local path>` | Uploads a YAML file to replace `config/default.yml`. Rejected if the data isn't valid base64 or valid YAML (a mapping). The previous file is kept as `config/default.yml.bak`. Used at the next `start`. | `ACK:setconfig (<n> bytes written, ...)` |
| `camera.start` | Starts the Alvium camera thread, if it isn't already running (for `autostart_camera: False`). | `ACK:camera.start` |
| `camera.capture [-e <µs>] [-g <dB>]` | Captures one Alvium frame (defaults: exposure 10000, gain 30). Slow: the image travels over the radio. | `ACK:... capturing...`, then `IMGDATA:<base64>`, saved as `captured_frame.jpg` on the ground |
| `gimbal.goto <yaw> <pitch> <roll>` | Moves the gimbal (degrees). Needs the pointing controller. | `ACK:`/`ERR:` |
| `gimbal.mode <off\|lock\|follow>` | Sets the gimbal mode. | `ACK:`/`ERR:` |
| `gimbal.starttrack` / `gimbal.stoptrack` | Starts or stops POI tracking. | `ACK:`/`ERR:` |
| `$<shell command>` | Runs a shell command on the Pi. If it finishes within 2 s, the output (first 2000 bytes) comes back directly. Otherwise it becomes a background job. | `CMDOUT:<base64>`, or `ACK:job <id> started ...` then `ACK:job done <id>:<status>:<time>:<base64>` |
| `jobs` | Lists running background jobs. | `ACK:jobs ...` |
| `canceljob <id>` | Sends SIGTERM to a background job. | `ACK:canceljob <id> sent SIGTERM` |
| `reboot` / `shutdown` | Reboots or powers off the Pi after 3 s. | `ACK:reboot` / `ACK:shutdown` |

`camera.*` and `gimbal.*` are relayed to porter through `/tmp/porter_cmd.sock`, so they only work while porter is running with that device configured. Background jobs have no time limit; cancel long-running ones with `canceljob`.

## Telemetry packet (TEL)

Once per second telemd sends `TEL:<json>` with:

| Key | Content | Source |
|---|---|---|
| `system` | `time` (Unix time), `hddusd`/`hddfree`/`hddtot` (GiB, filesystem `/`), `cputemp` (°C, `/sys/class/thermal/thermal_zone0/temp`) | telemd, always present |
| `health` | Per sensor/camera: `state` (`ok`/`stale`/`dead`), `age_s`, extra fields, and `error` if it failed | porter, only while running |
| `meta` | Per-sensor extra data, e.g. GPS fix and position | porter, only while running |
| `porter` | `running (porter@<name>.service)` or `stopped` | telemd (`systemctl`) |
| `chrony` | `ok`, reference name, whether it's PPS | telemd |
| `power_controller` | INA228 readings | only if `POWER_CONTROLLER_ENABLED` (off: powerd is not implemented) |
| `sid`, `seq` | Session ID (new at every telemd start) and a sequence number | telemd |

**Only the newest telemetry is ever sent.** The scheduler overwrites a single slot instead of queueing, so on a weak link TEL packets are skipped rather than delayed. When the link returns, the first packet received is current data, not a backlog. Command replies use a separate queue and are never dropped.

On the ground, `remote_host.py` ignores any TEL with the same `sid` and a `seq` not newer than the last one shown, and displays the **Telemetry age**, measured with the ground laptop's own clock:

| Colour | Age |
|---|---|
| green | under 3 s |
| yellow | 3–10 s |
| red | 10 s or more |

## Radio protocol

- **Messages** are UTF-8 text ending with the byte `0x00` (`END_OF_MESSAGE_BYTE`). Long messages are split into 80-byte radio packets (`MAX_PACKET_SIZE`) and reassembled by the receiver.
- **Delivery:** packets are sent unicast with ACK and retries, to the address in `Xbee.IDs` (`OBI` = ground, `LUKE` = payload), at transmit power level 4 (the maximum).
- **Gaps:** if more than 1 s passes between two packets of the same message, the receiver drops the incomplete message instead of joining it to the next one. The ground log shows `[discarding incomplete message after RX gap]`.
- **Binary data** (`LOGDATA`, `IMGDATA`, `CMDOUT`, `setconfig`) is base64-encoded.

## Ground station: remote_host.py

Run on the laptop with the ground XBee connected:

```sh
python3 remote_host.py [--port /dev/ttyUSB0] [--baudrate 38400] [--debug]
```

It needs `digi-xbee` installed on the laptop.

**Upper panel**, from the top:
- **Time, disk and CPU:** payload time, free/total disk, and CPU temperature (green below 70 °C, yellow 70–80 °C, red from 80 °C, where the Pi throttles).
- **Link and porter:** downlink RSSI and porter status.
- **Telemetry age.**
- **Chrony:** status and time reference.
- **Power controller:** status.
- **Health:** one line per sensor and camera.

**Lower panel:** the log of sent and received messages, and the command prompt. Type `help` for the command list, and `quit` or `q` to exit. Files received from the payload (`flight.log`, `captured_frame.jpg`) are saved in the folder where `remote_host.py` was started.

See the root [README.md](../README.md) for how telemd and porter fit together.
