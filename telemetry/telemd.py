#!/usr/bin/env python3
"""
telemd.py  -  payload XBee radio daemon.

Starts at boot via systemd.  Owns the XBee device and listens for commands
from the ground station.  All device I/O runs in a single thread to avoid
concurrent-access issues with the digi-xbee library.

Supported commands (uplink, ground → payload):
  ping             reply "pong"
  getlog           send last 40kB of flight.log (base64-encoded)
  reboot           reboot the computer
  setconfig <path> overwrite config/default.yml with base64-encoded data
  shutdown         shut down the computer
  start            start control.py in the project venv
  stop             gracefully stop control.py
  camera.start     start a camera thread (Alvium only)
  camera.capture [-e <exposure>] [-g <gain>]  capture a single frame from the Alvium camera
  gimbal.goto <yaw> <pitch> <roll>  move gimbal to specified angles (degrees)
  gimbal.mode <mode>  set gimbal mode (off, lock, or follow)
  gimbal.starttrack  start pointing controller POI tracking (if configured)
  gimbal.stoptrack   stop pointing controller POI tracking
  $<shell command> run a shell command and return its output (base64-encoded
Every message ends with END_OF_MESSAGE_BYTE (0x00) as defined in Xbee.py.
"""

import base64
import json
import logging
import os
import shlex
import subprocess
import sys
import queue
import signal
import threading
import socket
import tempfile
import time
import uuid
import re
import yaml
import shutil

# global state for jobs that are running in the background (e.g. shell commands)
_jobs: dict[str, dict] = {}     # job_id -> {"proc", "outfile", "cmd", "start"}
_jobs_lock = threading.Lock()
JOB_POLL_INTERVAL = 0.5

# path setup
TELEMD_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, TELEMD_DIR)

# add porter_venv to sys.path so we can import parameters.py
PORTER_DIR = os.path.join(TELEMD_DIR, "..")
sys.path.insert(1, PORTER_DIR)
import parameters as params

from digi.xbee.exception import TimeoutException
from Xbee import Xbee, TransmitException, END_OF_MESSAGE_BYTE

# configuration 
XBEE_PORT     = "/dev/ttyUSB0"
XBEE_BAUDRATE = 38400
REMOTE_NAME   = "OBI"    # key in Xbee.IDs for the ground station
READ_TIMEOUT  = 0.2      # seconds per read_data call

PORTER_UNIT_TEMPLATE = "porter@.service"
VENV_PYTHON   = os.path.join(params.home_directory, "porter_venv", "bin", "python3")
STATUS_FILE   = "/tmp/porter_status.json"
LOG_FILE      = os.path.join(params.home_directory, params.data_folder_name, params.current_symlink_name, params.logfile_name)
CONFIG_DIR = os.path.join(TELEMD_DIR, "..", "config")
CONFIG_FILE   = os.path.join(CONFIG_DIR, "default.yml")
LOG_TAIL_BYTES    = 40960  # bytes sent in response to 'getlog'
SHELL_CMD_TIMEOUT = 10     # seconds before a shell command is killed
SHELL_QUICK_WAIT = 2.0     # seconds to wait before treating a command as a background job
CMDOUT_MAX_BYTES  = 2000   # output cap before base64 encoding
CAPTURE_SCRIPT  = os.path.join(TELEMD_DIR, "alvium_capture.py")
CAPTURE_OUTPUT  = os.path.join(TELEMD_DIR, "captured_frame.jpg")
CAPTURE_TIMEOUT = 60        # seconds to wait for a frame to be captured
CHRONY_TIMEOUT        = 2   # seconds to wait for a chronyc call
CHRONY_POLL_INTERVAL  = 5   # seconds between chrony status polls
MAX_IO_ERRORS = 25          # consecutive XBee errors (about 5 s) before giving up

CMD_SOCKET_PATH = "/tmp/porter_cmd.sock"
CMD_TIMEOUT = 5.0

# status of powerd daemon
POWER_CONTROLLER_ENABLED = False
POWER_CONTROLLER_STATUS_PATH = "/tmp/power_cmd.json"

# logging
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_stream_handler])
logger = logging.getLogger("telemd")

# shared state
_shutdown = threading.Event()
_outbound: "queue.Queue[str]" = queue.Queue()   # messages to send

_chrony_lock  = threading.Lock()
_chrony_cache: dict = {"ok": False, "error": "not polled yet"}

# telemetry packet (one slot, not a queue, to avoid flooding the XBee with old telemetry packets)
_latest_tel: "str | None" = None
_tel_lock = threading.Lock()
_tel_session = uuid.uuid4().hex[:4]   # changes at every telemd restart
_tel_seq = 0

# set when telemd must exit with an error so systemd restarts it
_crashed = threading.Event()

# slow commands (start/stop/capture) run here, off the radio thread
_slow_jobs: "queue.Queue" = queue.Queue()


def _systemctl(*args, timeout=30):
    return subprocess.run(["sudo", "systemctl", *args],
                          capture_output=True, text=True, timeout=timeout)

def _active_porter_units() -> list[str]:
    try:
        r = subprocess.run(["systemctl", "list-units", "--type=service", 
                            "--state=active,activating,deactivating,reloading",
                            "--no-legend", "--plain", "porter@*"],
                        capture_output=True, text=True, timeout=5)
    except Exception as e:
        logger.warning(f"_active_porter_units: systemctl failed: {e}")
        return []
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


def _send_command(cmd: str, params: dict | None = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(CMD_TIMEOUT)
        sock.connect(CMD_SOCKET_PATH)
        sock.sendall((json.dumps({"cmd": cmd, "params": params or {}}) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))

def _porter_start(config_name: str = "default") -> str:
    # config_name is relative to config/, without .yml, e.g. "default" or "test_configs/starspec"
    if not re.fullmatch(r"[A-Za-z0-9_\-/]+", config_name) or ".." in config_name:
        return f"ERR:porter_start invalid config name {config_name!r}"
    if not os.path.isfile(os.path.join(CONFIG_DIR, config_name + ".yml")):
        return f"ERR:porter_start config/{config_name}.yml not found"
    active = _active_porter_units()
    if active:
        return f"ERR:porter already running ({active[0]})"
    unit = subprocess.run(["systemd-escape", f"--template={PORTER_UNIT_TEMPLATE}", config_name],
                          capture_output=True, text=True, check=True).stdout.strip()
    r = _systemctl("start", unit)
    return f"ACK:porter_start {unit}" if r.returncode == 0 else f"ERR:porter_start {r.stderr.strip()}"



def _porter_stop() -> str:
    if not _active_porter_units():
        return "ERR:porter is not running"
    r = _systemctl("stop", "porter@*")        # blocks until stopped (max TimeoutStopSec)
    return "ACK:porter_stop" if r.returncode == 0 else f"ERR:porter_stop {r.stderr.strip()}"

# chrony 
def _query_chrony() -> dict:
    """Ask chronyd what it's currently synced to and whether it's PPS."""
    try:
        result = subprocess.run(
            ["chronyc", "tracking"],
            capture_output=True, text=True, timeout=CHRONY_TIMEOUT,
        )
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or "chronyc failed").strip()}

        info = {}
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            info[key.strip()] = val.strip()

        refid_line = info.get("Reference ID", "")
        m = re.search(r"\(([^)]+)\)", refid_line)
        ref_name = m.group(1) if m else refid_line or "unknown"

        offset_m = re.search(r"[-+]?\d*\.?\d+", info.get("Last offset", ""))
        offset_s = float(offset_m.group()) if offset_m else None

        try:
            stratum = int(info.get("Stratum", ""))
        except ValueError:
            stratum = None

        return {
            "ok": True,
            "ref": ref_name,
            "pps_selected": "pps" in ref_name.lower(),
            "stratum": stratum,
            "offset_s": offset_s,
            "leap_status": info.get("Leap status"),
        }
    except FileNotFoundError:
        return {"ok": False, "error": "chronyc not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "chronyc timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _chrony_monitor() -> None:
    logger.info("Chrony monitor started")
    while not _shutdown.is_set():
        try:
            result = _query_chrony()
            with _chrony_lock:
                global _chrony_cache
                _chrony_cache = result
            if not result.get("ok"):
                logger.warning(f"chrony query failed: {result.get('error')}")
        except Exception:
            logger.exception("Chrony monitor error")
        _shutdown.wait(CHRONY_POLL_INTERVAL)


def _camera_capture(exposure: int, gain: float) -> None:
    try:
        result = subprocess.run(
            ["bash", "--login", "-c",
             f'"{VENV_PYTHON}" "{CAPTURE_SCRIPT}" --exposure {exposure} --gain {gain}'],
            cwd=os.path.dirname(CAPTURE_SCRIPT),
            capture_output=True, text=True, timeout=CAPTURE_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "no output").strip()[:300]
            _outbound.put(f"ERR:camera.capture failed: {err}")
            return
        with open(CAPTURE_OUTPUT, "rb") as f:
            img_bytes = f.read()
        _outbound.put(f"IMGDATA:{base64.b64encode(img_bytes).decode('ascii')}")
        logger.info(f"Sending captured frame ({len(img_bytes)} bytes JPEG)")
    except subprocess.TimeoutExpired:
        _outbound.put("ERR:camera.capture timed out")
    except FileNotFoundError:
        _outbound.put("ERR:camera.capture: captured_frame.jpg not found after capture")
    except Exception as e:
        _outbound.put(f"ERR:camera.capture: {e}")


# command handler
def _handle(raw: bytes, antenna: Xbee) -> None:
    """Decode a complete received message and enqueue a reply."""
    cmd_casesensitive = raw.decode("utf-8", errors="replace").strip()
    cmd = cmd_casesensitive.lower()
    logger.info(f"RX: {cmd!r}")

    if cmd == "ping":
        try:
            value = antenna.device.get_parameter("DB")
            rssi_dbm = -int.from_bytes(value, byteorder='big')
            _outbound.put(f"pong - uplink RSSI: {rssi_dbm} dBm")
        except Exception as e:
            logger.warning(f"Failed to get RSSI: {e}")
            _outbound.put("ERR:uplink RSSI: unknown")

    elif cmd == "reboot":
        logger.warning("Reboot command received - rebooting in 3 s")
        _outbound.put("ACK:reboot")
        threading.Timer(3.0, lambda: subprocess.run(["sudo", "reboot"])).start()

    elif cmd == "shutdown":
        logger.warning("Shutdown command received - shutting down in 3 s")
        _outbound.put("ACK:shutdown")
        threading.Timer(3.0, lambda: subprocess.run(["sudo", "shutdown", "-h", "now"])).start()

    elif cmd == "start" or cmd.startswith("start "):
        parts = raw.decode("utf-8", errors="replace").split()
        config_name = parts[1] if len(parts) > 1 else "default"
        _outbound.put(f"ACK:start queued ({config_name})")
        _slow_jobs.put(lambda: _outbound.put(_porter_start(config_name)))

    elif cmd == "stop":
        _outbound.put("ACK:stop queued")
        _slow_jobs.put(lambda: _outbound.put(_porter_stop()))


    elif cmd == "getlog":
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, 2)  # seek to end
                size = f.tell()
                f.seek(max(0, size - LOG_TAIL_BYTES))
                tail = f.read()
            encoded = base64.b64encode(tail).decode("ascii")
            _outbound.put(f"LOGDATA:{encoded}")
            logger.info(f"Sending last {len(tail)} bytes of flight.log")
        except FileNotFoundError:
            _outbound.put("ERR:flight.log not found")
        except Exception as e:
            _outbound.put(f"ERR:getlog failed: {e}")

    elif cmd.startswith("camera.start"):
        try:
            resp = _send_command("camera.start")
            _outbound.put(f"ACK:camera.start {resp.get('detail','')}" if resp.get("ok")
                        else f"ERR:camera.start {resp.get('error','unknown error')}")
        except (ConnectionRefusedError, FileNotFoundError):
            _outbound.put("ERR:camera.start: command socket unavailable")
        except Exception as e:
            _outbound.put(f"ERR:camera.start: {e}")

    elif cmd.startswith("camera.capture"):
        raw_str = raw.decode("utf-8", errors="replace").strip()
        try:
            tokens = shlex.split(raw_str)
        except ValueError:
            tokens = raw_str.split()
        exposure, gain = 10000, 30.0
        i = 1
        while i < len(tokens):
            if tokens[i] == "-e" and i + 1 < len(tokens):
                try:
                    exposure = int(tokens[i + 1])
                except ValueError:
                    pass
                i += 2
            elif tokens[i] == "-g" and i + 1 < len(tokens):
                try:
                    gain = float(tokens[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1
        logger.info(f"Camera capture: exposure={exposure} gain={gain}")
        _outbound.put(f"ACK:camera.capture exposure={exposure} gain={gain} - capturing...")
        _slow_jobs.put(lambda: _camera_capture(exposure, gain))


    elif raw.startswith(b"gimbal.goto"):
        raw_str = raw.decode("utf-8", errors="replace").strip()
        try:
            tokens = shlex.split(raw_str)
        except ValueError:
            tokens = raw_str.split()
        yaw, pitch, roll = 0, 0, 0
        if len(tokens) >= 4:
            try:
                yaw = float(tokens[1])
                pitch = float(tokens[2])
                roll = float(tokens[3])
            except ValueError:
                _outbound.put("ERR:gimbal.goto: invalid angles")
                return
        try:
            resp = _send_command("gimbal.goto", {"yaw": yaw, "pitch": pitch, "roll": roll})
            _outbound.put(f"ACK:gimbal.goto {resp.get('detail','')}" if resp.get("ok")
                        else f"ERR:gimbal.goto {resp.get('error','unknown error')}")
        except (ConnectionRefusedError, FileNotFoundError):
            _outbound.put("ERR:gimbal.goto: command socket unavailable")
        except Exception as e:
            _outbound.put(f"ERR:gimbal.goto: {e}")

    elif raw.startswith(b"gimbal.mode"):
        raw_str = raw.decode("utf-8", errors="replace").strip()
        try:
            tokens = shlex.split(raw_str)
        except ValueError:
            tokens = raw_str.split()
        mode = "follow"
        if len(tokens) >= 2:
            mode = tokens[1]
        try:
            resp = _send_command("gimbal.mode", {"mode": mode})
            _outbound.put(f"ACK:gimbal.mode {resp.get('detail','')}" if resp.get("ok")
                        else f"ERR:gimbal.mode {resp.get('error','unknown error')}")
        except (ConnectionRefusedError, FileNotFoundError):
            _outbound.put("ERR:gimbal.mode: command socket unavailable")
        except Exception as e:
            _outbound.put(f"ERR:gimbal.mode: {e}")

    elif raw.startswith(b"gimbal.starttrack"):
        try:
            resp = _send_command("gimbal.starttrack")
            _outbound.put(f"ACK:gimbal.starttrack {resp.get('detail','')}" if resp.get("ok")
                        else f"ERR:gimbal.starttrack {resp.get('error','unknown error')}")
        except (ConnectionRefusedError, FileNotFoundError):
            _outbound.put("ERR:gimbal.starttrack: command socket unavailable")
        except Exception as e:
            _outbound.put(f"ERR:gimbal.starttrack: {e}")

    elif raw.startswith(b"gimbal.stoptrack"):
        try:
            resp = _send_command("gimbal.stoptrack")
            _outbound.put(f"ACK:gimbal.stoptrack {resp.get('detail','')}" if resp.get("ok")
                        else f"ERR:gimbal.stoptrack {resp.get('error','unknown error')}")
        except (ConnectionRefusedError, FileNotFoundError):
            _outbound.put("ERR:gimbal.stoptrack: command socket unavailable")
        except Exception as e:
            _outbound.put(f"ERR:gimbal.stoptrack: {e}")

    elif raw.startswith(b"$"):
        shell_cmd = raw[1:].decode("utf-8", errors="replace").strip()
        job_id = uuid.uuid4().hex[:8]
        logger.info(f"Shell cmd {job_id}: {shell_cmd!r}")
        try:
            outfile = tempfile.NamedTemporaryFile(
                delete=False, prefix=f"job_{job_id}_", suffix=".log", dir="/tmp"
            )
            proc = subprocess.Popen(
                shell_cmd,
                shell=True,
                stdout=outfile,
                stderr=subprocess.STDOUT,
                cwd=TELEMD_DIR,
                start_new_session=True,
            )
            outfile.close()

            try:
                retcode = proc.wait(timeout=SHELL_QUICK_WAIT)
                # finished quickly -> behave exactly like the old synchronous path
                with open(outfile.name, "rb") as f:
                    output = f.read()
                os.unlink(outfile.name)
                if not output:
                    output = f"(exit {retcode}, no output)".encode("utf-8")
                output = output[:CMDOUT_MAX_BYTES]
                encoded = base64.b64encode(output).decode("ascii")
                _outbound.put(f"CMDOUT:{encoded}")

            except subprocess.TimeoutExpired:
                # still running -> hand off to the background job tracker
                with _jobs_lock:
                    _jobs[job_id] = {
                        "proc": proc,
                        "outfile": outfile.name,
                        "cmd": shell_cmd,
                        "start": time.monotonic(),
                    }
                _outbound.put(f"ACK:job {job_id} started (PID {proc.pid}, still running): {shell_cmd}")

        except Exception as e:
            _outbound.put(f"ERR:shell failed: {e}")

    elif cmd == "jobs":
        with _jobs_lock:
            if not _jobs:
                _outbound.put("ACK:jobs none running")
            else:
                lines = []
                for jid, info in _jobs.items():
                    elapsed = time.monotonic() - info["start"]
                    lines.append(f"{jid} ({info['cmd'][:40]}) running {elapsed:.0f}s")
                _outbound.put("ACK:jobs" + " | ".join(lines))

    elif cmd.startswith("canceljob"):
        raw_str = raw.decode("utf-8", errors="replace").strip()
        tokens = raw_str.split()
        if len(tokens) < 2:
            _outbound.put("ERR:canceljob: missing job id")
        else:
            jid = tokens[1]
            with _jobs_lock:
                info = _jobs.get(jid)
            if info is None:
                _outbound.put(f"ERR:canceljob: no such job '{jid}'")
            else:
                try:
                    os.killpg(os.getpgid(info["proc"].pid), signal.SIGTERM)
                    _outbound.put(f"ACK:canceljob {jid} sent SIGTERM")
                except ProcessLookupError:
                    _outbound.put(f"ERR:canceljob {jid}: process already gone")
                except Exception as e:
                    _outbound.put(f"ERR:canceljob {jid}: {e}")

    elif raw.startswith(b"setconfig"):
        try:
            encoded = raw[len(b"setconfig"):].lstrip(b": ")
            data = base64.b64decode(encoded, validate=True)
            if not data:
                raise ValueError("empty config")
            parsed = yaml.safe_load(data)
            if not isinstance(parsed, dict):
                raise ValueError("config is not a YAML mapping")
            if os.path.exists(CONFIG_FILE):
                shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, CONFIG_FILE)
            _outbound.put(f"ACK:setconfig ({len(data)} bytes written, backup in default.yml.bak)")
            logger.info(f"config/default.yml overwritten ({len(data)} bytes)")
        except Exception as e:
            _outbound.put(f"ERR:setconfig rejected: {e}")


    else:
        reply = f"ERR:unknown command '{cmd_casesensitive}'"
        logger.warning(reply)
        _outbound.put(reply)


# XBee I/O thread
def _io_thread(antenna: Xbee) -> None:
    """
    The ONLY thread that calls read_data / send_data on the device.
    Loop:
      1. Drain outbound queue → send each message.
      2. read_data(short timeout) → accumulate into rx_buf.
      3. When rx_buf ends with EOM byte → dispatch complete message.
    """
    logger.info("I/O thread started")
    global _latest_tel
    rx_buf = b""
    last_frame_time = 0.0
    io_errors = 0


    while not _shutdown.is_set():

        # send everything queued
        while True:
            try:
                msg = _outbound.get_nowait()
            except queue.Empty:
                break
            try:
                if antenna.remote_device is not None:
                    antenna.send_msg(msg)
                else:
                    antenna.send_msg_broadcast(msg)
                logger.info(f"TX: {msg!r}")
            except TransmitException as e:
                logger.warning(f"Transmit failed: {e}")
            except Exception as e:
                logger.error(f"Send error: {e}")

        # then the newest telemetry only, without ACK/retries: an old telemetry is never worth retrying
        with _tel_lock:
            tel, _latest_tel = _latest_tel, None
        if tel is not None:
            try:
                if antenna.remote_device is not None:
                    antenna.send_msg(tel, ack=True)
                else:
                    antenna.send_msg_broadcast(tel)
            except Exception as e:
                logger.warning(f"Telemetry message dropped: {e}")


        # try to receive one frame
        try:
            frame = antenna.device.read_data(timeout=READ_TIMEOUT)
            io_errors = 0
        except TimeoutException:
            frame = None
            io_errors = 0          # a timeout just means "nothing received"
        except Exception as e:
            frame = None
            io_errors += 1
            if not _shutdown.is_set():
                logger.warning(f"read_data error ({io_errors}/{MAX_IO_ERRORS}): {e}")
            if io_errors >= MAX_IO_ERRORS:
                logger.critical("XBee not responding, exiting so systemd restarts telemd")
                _crashed.set()
                _shutdown.set()
                break
            time.sleep(READ_TIMEOUT)   # don't spin at 100% CPU


        if frame is not None:
            now = time.monotonic()
            if rx_buf and now - last_frame_time > 1.0:
                logger.warning("Discarding incomplete message after RX gap")
                rx_buf = b""
            last_frame_time = now
            rx_buf += frame.data

            if rx_buf.endswith(END_OF_MESSAGE_BYTE):
                try:
                    _handle(rx_buf[:-1], antenna)
                except Exception as e:
                    logger.error(f"Error handling message: {e}")
                    _outbound.put(f"ERR:Error handling message: {e}")
                rx_buf = b""
            elif len(rx_buf) > 80 * 1024:
                logger.warning("RX buffer overflow, discarding")
                rx_buf = b""

    logger.info("I/O thread stopped")

def _read_cpu_temp() -> "float | None":
    """CPU temperature in °C from the Pi's thermal sensor, or None if unavailable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)   # value is in millidegrees
    except (OSError, ValueError):
        return None

def _read_status() -> dict:
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _read_powerd_status() -> dict:
    try:
        with open(POWER_CONTROLLER_STATUS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _telemetry_scheduler() -> None:
    """Send a telemetry packet every second. No device access — only enqueues."""
    logger.info("Telemetry scheduler started")
    global _tel_seq, _latest_tel

    while not _shutdown.is_set():
        _shutdown.wait(1.0)
        try:
            status = _read_status()

            total, used, free = shutil.disk_usage("/")
            status["system"] = {
                "time":    time.time(),
                "hddusd":  round(used  / (1024 ** 3), 2),
                "hddfree": round(free  / (1024 ** 3), 2),
                "hddtot":  round(total / (1024 ** 3), 2),
                "cputemp": _read_cpu_temp(),
            }

            if POWER_CONTROLLER_ENABLED:
                powerd_status = _read_powerd_status()
                status["power_controller"] = powerd_status
            with _chrony_lock:
                status["chrony"] = dict(_chrony_cache)

            active = _active_porter_units()
            status["porter"] = f"running ({active[0]})" if active else "stopped"

            _tel_seq += 1
            status["sid"] = _tel_session
            status["seq"] = _tel_seq
            tel = f"TEL:{json.dumps(status, separators=(',', ':'))}"
            with _tel_lock:
                _latest_tel = tel        # overwrite, never queue: only the newest telemetry is sent
        except Exception as e:
            logger.exception(f"Telemetry scheduler error: {e}")


def _job_monitor() -> None:
    """Poll running background jobs; report + clean up finished ones."""
    logger.info("Job monitor started")
    while not _shutdown.is_set():
        _shutdown.wait(JOB_POLL_INTERVAL)
        try:
            with _jobs_lock:
                job_ids = list(_jobs.keys())

            for jid in job_ids:
                with _jobs_lock:
                    info = _jobs.get(jid)
                if info is None:
                    continue

                proc = info["proc"]
                retcode = proc.poll()
                if retcode is None:
                    continue   # still running

                try:
                    with open(info["outfile"], "rb") as f:
                        output = f.read()
                except FileNotFoundError:
                    output = b""
                finally:
                    try:
                        os.unlink(info["outfile"])
                    except FileNotFoundError:
                        pass

                elapsed = time.monotonic() - info["start"]
                output = output[:CMDOUT_MAX_BYTES]
                encoded = base64.b64encode(output).decode("ascii")
                status = "ok" if retcode == 0 else f"exit {retcode}"
                _outbound.put(f"ACK:job done {jid}:{status}:{elapsed:.1f}s:{encoded}")
                logger.info(f"Job {jid} finished ({status}, {elapsed:.1f}s)")

                with _jobs_lock:
                    _jobs.pop(jid, None)
        except Exception as e:
            logger.exception(f"Job monitor error: {e}")

    logger.info("Job monitor stopped")

def _slow_worker() -> None:
    """Runs slow commands one at a time, so the radio thread never blocks."""
    logger.info("Slow-command worker started")
    while not _shutdown.is_set():
        try:
            job = _slow_jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            job()
        except Exception as e:
            logger.exception("Slow command failed")
            _outbound.put(f"ERR:{e}")
    logger.info("Slow-command worker stopped")


# main
def main() -> None:
    def _on_signal(signum, frame):
        logger.info(f"Caught {signal.strsignal(signum)}, shutting down")
        _shutdown.set()

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(f"telemd starting on {XBEE_PORT} @ {XBEE_BAUDRATE} baud, remote={REMOTE_NAME}")

    antenna = Xbee(port=XBEE_PORT, baudrate=XBEE_BAUDRATE)
    antenna.open(force_settings=True, remote_name=REMOTE_NAME)
    logger.info("XBee open")

    _outbound.put("telemd started")   # announce we are alive

    threads = [
        threading.Thread(target=_io_thread, args=(antenna,), daemon=True, name="io"),
        threading.Thread(target=_telemetry_scheduler,        daemon=True, name="telemetry"),
        threading.Thread(target=_job_monitor,                daemon=True, name="jobs"),
        threading.Thread(target=_chrony_monitor,             daemon=True, name="chrony"),
        threading.Thread(target=_slow_worker,                daemon=True, name="worker"),
    ]
    for t in threads:
        t.start()

    # watch the threads: if one dies, exit with an error so systemd restarts telemd
    while not _shutdown.wait(2.0):
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            logger.critical(f"telemd thread(s) died: {dead}")
            _crashed.set()
            _shutdown.set()

    logger.info("Closing XBee...")
    try:
        antenna.close()
    except Exception:
        pass
    logger.info("telemd stopped")
    sys.exit(1 if _crashed.is_set() else 0)



if __name__ == "__main__":
    main()