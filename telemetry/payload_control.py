"""
payload_control.py  -  porter control shared by the two links.

telemd (XBee) and rcd (DJI Pilot 2 widgets through psdk-bridge) both import it: starting and stopping porter,
relaying commands to porter's CommandServer, and the status they report. Nothing here touches a radio.
start/stop hold a file lock, so the two daemons never start or stop porter at the same time.
"""

import fcntl
import os
import subprocess
import shutil
import logging
import socket
import json
import yaml
import re
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# path setup
TELEMETRY_DIR = os.path.dirname(os.path.realpath(__file__))

PORTER_UNIT_TEMPLATE = "porter@.service"
STATUS_FILE   = "/tmp/porter_status.json"
CONFIG_DIR = os.path.join(TELEMETRY_DIR, "..", "config")

CMD_SOCKET_PATH = "/tmp/porter_cmd.sock"
CMD_TIMEOUT = 5.0

# one start/stop at a time, across telemd and rcd
CONTROL_LOCK_FILE    = "/run/lock/porter-control.lock"   # /run/lock: tmpfs, mode 1777. Never delete the file
CONTROL_LOCK_TIMEOUT = 30.0                              # longer than one porter stop (TimeoutStopSec=20)

CHRONY_TIMEOUT = 2   # seconds to wait for a chronyc call

GPS_FIX_NAMES = {0: "no fix", 1: "DR", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "time"}

def systemctl(*args, timeout=30):
    return subprocess.run(["sudo", "systemctl", *args],
                          capture_output=True, text=True, timeout=timeout)

def active_porter_units() -> list[str]:
    try:
        r = subprocess.run(["systemctl", "list-units", "--type=service", 
                            "--state=active,activating,deactivating,reloading",
                            "--no-legend", "--plain", "porter@*"],
                        capture_output=True, text=True, timeout=5)
    except Exception as e:
        logger.warning(f"active_porter_units: systemctl failed: {e}")
        return []
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]

@contextmanager
def _control_lock(timeout: float):
    """Exclusive lock on CONTROL_LOCK_FILE; raises TimeoutError after timeout seconds.
    Released when the file is closed, also if the process dies."""
    fd = os.open(CONTROL_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)

def porter_start(config_name: str = "default") -> str:
    # waits for a start/stop running in the other daemon, e.g. an RC start during an XBee stop
    try:
        with _control_lock(CONTROL_LOCK_TIMEOUT):
            return _porter_start_locked(config_name)
    except TimeoutError:
        return f"ERR:porter_start another start/stop still running after {CONTROL_LOCK_TIMEOUT:.0f} s"

def _porter_start_locked(config_name: str) -> str:
    # config_name is relative to config/, without .yml, e.g. "default" or "test_configs/starspec"
    if not re.fullmatch(r"[A-Za-z0-9_\-/]+", config_name) or ".." in config_name:
        return f"ERR:porter_start invalid config name {config_name!r}"
    if not os.path.isfile(os.path.join(CONFIG_DIR, config_name + ".yml")):
        return f"ERR:porter_start config/{config_name}.yml not found"
    try:
        with open(os.path.join(CONFIG_DIR, config_name + ".yml")) as f:
            cfg = yaml.safe_load(f) or {}
        min_gb = float(cfg.get("global", {}).get("disk_guard", {}).get("start_min_free_gb", 0.3))
    except Exception:
        min_gb = 0.8
    free_gb = shutil.disk_usage("/").free / 1024**3
    if free_gb < min_gb:
        return f"ERR:porter_start only {free_gb:.1f} GB free, need {min_gb:.1f} GB"
    active = active_porter_units()
    if active:
        return f"ERR:porter already running ({active[0]})"
    unit = subprocess.run(["systemd-escape", f"--template={PORTER_UNIT_TEMPLATE}", config_name],
                          capture_output=True, text=True, check=True).stdout.strip()
    r = systemctl("start", unit)
    return f"ACK:porter_start {unit}" if r.returncode == 0 else f"ERR:porter_start {r.stderr.strip()}"

def porter_stop() -> str:
    try:
        with _control_lock(CONTROL_LOCK_TIMEOUT):
            if not active_porter_units():
                return "ERR:porter is not running"
            r = systemctl("stop", "porter@*")        # blocks until stopped (max TimeoutStopSec)
            return "ACK:porter_stop" if r.returncode == 0 else f"ERR:porter_stop {r.stderr.strip()}"
    except TimeoutError:
        return f"ERR:porter_stop another start/stop still running after {CONTROL_LOCK_TIMEOUT:.0f} s"

def send_command(cmd: str, params: dict | None = None, timeout: float = CMD_TIMEOUT) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(CMD_SOCKET_PATH)
        sock.sendall((json.dumps({"cmd": cmd, "params": params or {}}) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))

def read_cpu_temp() -> "float | None":
    """CPU temperature in °C from the Pi's thermal sensor, or None if unavailable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)   # value is in millidegrees
    except (OSError, ValueError):
        return None

def read_status() -> dict:
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def query_chrony() -> dict:
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