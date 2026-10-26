# services

systemd units and boot scripts for running PORTER on the payload's Raspberry Pi 5. All units assume user `polocalc`, the repository at `/home/polocalc/flight/porter` and the venv at `/home/polocalc/porter_venv`.

| File | Installed by `install_modules.py` | Enabled at boot |
|---|---|---|
| `telemd.service` + `99-xbee.rules` | yes | yes, while the XBee is plugged in |
| `porter@.service` | yes | no, started by telemd or rcd |
| `psdk-bridge.service` | yes (`install_porter.sh`) | yes, if `psdk_telemetry` was built (Matrice 400) |
| `rcd.service` | yes (`install_porter.sh`) | yes, if `psdk_telemetry` was built (Matrice 400) |
| `i2c-config.service` + `i2c_startup_config.sh` | no, optional, by hand | optional |
| `powerd.service` | no, powerd is not implemented | no |

## telemd.service

Runs `telemetry/telemd.py`, the XBee radio daemon, while the XBee is plugged in.

- **Bound to the XBee:** `99-xbee.rules` (in `/etc/udev/rules.d/`) names the radio `/dev/xbee` and creates `dev-xbee.device`. `telemd.service` has `BindsTo=dev-xbee.device` and `WantedBy=dev-xbee.device`: systemd starts it when the XBee appears, at boot too, and stops it when it is unplugged. Without the XBee, telemd doesn't run, and nothing else depends on it.
- **The rule matches the USB adapter's serial number**, so no other USB-serial device can be taken for the XBee. With the radio plugged in, `udevadm info -a -n /dev/ttyUSB0 | grep -E 'idVendor|serial'` gives the values; replace `CHANGE_ME`, then `sudo cp services/99-xbee.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty`.
- **On/off:** `sudo systemctl disable --now telemd` keeps it off even with the XBee plugged in; `enable` turns it back on. Don't `systemctl start telemd` without the XBee: it waits for the device for 90 s, then fails.
- **Restarts:** `Restart=on-failure` with a 5 s delay. telemd exits with an error when the XBee stops responding or one of its threads dies, so it recovers without a reboot.
- **Logs:** `journalctl -u telemd -f`

## porter@.service

Template unit for `control.py`. The part after `@` selects the configuration, relative to `config/` and without `.yml`:

```sh
sudo systemctl start porter@default                  # control.py -c config/default.yml
sudo systemctl start porter@test_configs-starspec    # control.py -c config/test_configs/starspec.yml
sudo systemctl stop 'porter@*'
journalctl -u 'porter@*' -f
```

In an instance name, `-` stands for `/`. For a file name containing a real `-`, use `systemd-escape --template=porter@.service <name>` to get the unit name.

What the settings do:

- **`ExecStart`:** runs `bash --login -c 'exec .../python3 control.py -c "config/%I.yml"'`.
  - `--login` loads `~/.profile`, which provides `~/.local/bin` (the sensor binaries) and the Vimba/GenICam variables.
  - `exec` makes Python the main process, so it receives the stop signal directly.
- **`KillMode=mixed` + `KillSignal=SIGINT`:** on stop, only `control.py` receives SIGINT and shuts down cleanly, stopping its sensor binaries itself.
- **`TimeoutStopSec=20`:** after 20 s, systemd SIGKILLs anything left in the unit, including sensor binaries in their own process groups. `control.py`'s own shutdown (`SHUTDOWN_TIMEOUT` = 12 s, plus 3 s for leftover binaries) fits within this limit.
- **`Restart=no`:** every start creates a new run folder, so porter is not restarted automatically.
- **No `[Install]` section:** not started at boot. telemd or rcd starts it on the `start` command.

## psdk-bridge.service

Matrice 400 only. Runs `psdk_telemetry --service` from boot (the program is in `modules/lager/modules/Payload-SDK`, see its README):
- connects to the drone through the E-Port (`/dev/ttyGS0`, from `dji-vcom-gadget.service`) and requests 24 V on the E-Port VCC,
- shows the POLOCALC widgets in DJI Pilot 2: **Start acquisition**, **Stop acquisition**, **POI tracking**, **Shutdown Pi** (press twice within 5 s), the **Configuration** list (the `.yml` files of `config/`, read when the service starts), and a status line in the floating window,
- serves the drone telemetry to lager on `/tmp/psdk_telemetry.sock` (`socket:` in the `uav.connection` block of `config/default_M400.yml`),
- sends the RC commands on `/tmp/psdk_control.sock` to rcd, which runs them and sends back the status line (its messages still say "telemd": they mean whatever client is connected, now rcd).

The topic rates are set on its `ExecStart` line. `Restart=always` with a 10 s delay covers "no drone yet" and "link lost". On a Matrice 600 setup it isn't needed: `sudo systemctl disable --now psdk-bridge rcd`. Logs: `journalctl -u psdk-bridge -f`.

## rcd.service

Matrice 400 only. Runs `telemetry/rcd.py` from boot: connects to `/tmp/psdk_control.sock`, runs the widget commands (start, stop, POI tracking, shutdown) and sends the status line every 2 s. Independent of telemd and the XBee.

- **`Wants=psdk-bridge.service`, not `BindsTo`:** psdk-bridge restarts whenever the drone link drops; rcd notices, waits and reconnects within 5 s instead of being restarted.
- **One start/stop at a time:** telemd and rcd start and stop porter through `telemetry/payload_control.py`, which holds `/run/lock/porter-control.lock` meanwhile. A command from one link waits up to 30 s for the other's to finish. `flock /run/lock/porter-control.lock sleep 20` holds it by hand, for testing.
- **Restarts:** `Restart=on-failure` with a 5 s delay, if one of its threads dies.
- **Logs:** `journalctl -u rcd -f`

Don't add `PrivateTmp=` to any of these units: telemd, rcd, porter and psdk-bridge share `/tmp/porter_status.json`, `/tmp/porter_cmd.sock` and the two `/tmp/psdk_*.sock` sockets.

## i2c-config.service (optional)

A one-shot unit, run before `zkbootrtc.service`, that executes `/usr/local/bin/i2c_startup_config.sh`. Despite the name, the script doesn't configure I2C: it sets the CPU frequency governor to `performance` on cores 0–3, for steadier sensor timing. The installer doesn't install it. To use it:

```sh
sudo cp services/i2c_startup_config.sh /usr/local/bin/
sudo cp services/i2c-config.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now i2c-config.service
```

## powerd.service (do not install)

Unit for `power_monitor/powerd.py`, which is not implemented yet (see [power_monitor/README.md](../power_monitor/README.md)). If enabled now, it would crash and restart in a loop.

## After changing a unit file

```sh
sudo cp services/<unit> /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart telemd        # for telemd.service (or rcd); porter@ picks up changes at its next start
```

Running `install_porter.sh` again does the same for all the units.

See the root [README.md](../README.md) for the overall architecture.