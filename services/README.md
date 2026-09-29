# services

systemd units and boot scripts for running PORTER on the payload's Raspberry Pi 5. All units assume user `polocalc`, the repository at `/home/polocalc/flight/porter` and the venv at `/home/polocalc/porter_venv`.

| File | Installed by `install_modules.py` | Enabled at boot |
|---|---|---|
| `telemd.service` | yes | yes |
| `porter@.service` | yes | no, started by telemd |
| `i2c-config.service` + `i2c_startup_config.sh` | no, optional, by hand | optional |
| `powerd.service` | no, powerd is not implemented | no |

## telemd.service

Runs `telemetry/telemd.py`, the XBee radio daemon, from boot.

- **Waits for the XBee:** `ExecStartPre=/bin/sleep 5` gives the USB device time to appear.
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
- **No `[Install]` section:** not started at boot. telemd starts it on the `start` command.

Don't add `PrivateTmp=` to either unit: telemd and porter share `/tmp/porter_status.json` and `/tmp/porter_cmd.sock`.

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
sudo systemctl restart telemd        # for telemd.service; porter@ picks up changes at its next start
```

Running `python3 install_modules.py` again does the same for `telemd.service` and `porter@.service`.

See the root [README.md](../README.md) for the overall architecture.
