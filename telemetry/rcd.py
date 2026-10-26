#!/usr/bin/env python3
"""
rcd.py  -  RC link daemon (Matrice 400): DJI Pilot 2 widgets <-> porter.

Runs as rcd.service.  Connects to psdk-bridge's control socket (psdk_telemetry --service),
runs the widget commands and keeps the status line in the RC's floating window up to date.
Independent of telemd and of the XBee: starting/stopping porter comes from payload_control.py,
whose lock keeps the two links from starting or stopping porter at the same time.

Control socket, one line each way (see service.h in the Payload-SDK sample):
  psdk-bridge -> rcd:  CMD start <config> | CMD stop | CMD poi on | CMD poi off | CMD shutdown
  rcd -> psdk-bridge:  TEXT <status line> | MSG <reply> | STATE <running> <poi>
"""

import logging
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

# path setup
TELEMETRY_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, TELEMETRY_DIR)

# shared with telemd.py: porter start/stop, command socket, status
import payload_control as pctrl

# configuration
PSDK_CONTROL_SOCKET  = "/tmp/psdk_control.sock"
RC_STATUS_PERIOD     = 2.0   # seconds between status lines sent to the RC
RC_RETRY_PERIOD      = 5.0   # seconds between connection attempts while psdk-bridge isn't there
RC_TEXT_MAX          = 250   # the RC shows at most 255 characters
CHRONY_POLL_INTERVAL = 5     # seconds between chrony status polls

# logging
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_stream_handler])
logger = logging.getLogger("rcd")

# shared state
_shutdown = threading.Event()
_crashed = threading.Event()        # set when rcd must exit with an error so systemd restarts it
_commands: "queue.Queue" = queue.Queue()   # RC commands, run one at a time by the worker

_chrony_lock  = threading.Lock()
_chrony_cache: dict = {"ok": False, "error": "not polled yet"}

_rc_frames = {}     # camera name -> (frames_captured, monotonic time), for the frame rate


# chrony
def _chrony_monitor() -> None:
    logger.info("Chrony monitor started")
    while not _shutdown.is_set():
        try:
            result = pctrl.query_chrony()
            with _chrony_lock:
                global _chrony_cache
                _chrony_cache = result
        except Exception:
            logger.exception("Chrony monitor error")
        _shutdown.wait(CHRONY_POLL_INTERVAL)


def _sync_token() -> str:
    """PPS, the chrony reference name, or NOSYNC: short enough for the RC line."""
    with _chrony_lock:
        c = dict(_chrony_cache)
    if not c.get("ok") or c.get("leap_status") == "Not synchronised":
        return "NOSYNC"
    return "PPS" if c.get("pps_selected") else str(c.get("ref", "?"))[:8]


# status line
def _unit_uptime(unit: str) -> str:
    """mm:ss (or h:mm:ss) since the systemd unit became active, '' if unknown."""
    try:
        r = pctrl.systemctl("show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value", timeout=5)
        seconds = int(time.monotonic() - int(r.stdout.strip()) / 1e6)
    except Exception:
        return ""
    h, rest = divmod(max(seconds, 0), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def _rc_status() -> tuple:
    """(porter running, POI tracking, one-line summary) for the RC's floating window."""
    active = pctrl.active_porter_units()
    parts, poi = [], False
    if active:
        unit = active[0]
        config = unit.split("@", 1)[1].rsplit(".service", 1)[0] if "@" in unit else unit
        parts.append(f"RUN {config} {_unit_uptime(unit)}".rstrip())
        health = pctrl.read_status().get("health", {})
        now = time.monotonic()
        for name, info in health.items():
            if not isinstance(info, dict):
                continue
            if "frames_captured" in info:                       # camera
                frames = info["frames_captured"]
                last = _rc_frames.get(name)
                _rc_frames[name] = (frames, now)
                if last and now > last[1] and frames >= last[0]:
                    parts.append(f"CAM {(frames - last[0]) / (now - last[1]):.0f}fps")
            elif "sats" in info and "status" in info:           # u-blox GPS
                parts.append(f"GPS {pctrl.GPS_FIX_NAMES.get(info['status'], info['status'])} {info['sats']}")
        uav = health.get("UAV", {})
        if isinstance(uav, dict) and uav.get("rtk") is not None:
            parts.append(f"RTK {uav['rtk']}")
        if isinstance(uav, dict) and uav.get("bat") is not None:
            parts.append(f"BAT {uav['bat']}%")
        poi_info = health.get("POI", {})
        if isinstance(poi_info, dict) and poi_info.get("tracking"):
            poi = True
            distance = poi_info.get("distance")
            parts.append(f"POI on {distance:.0f}m" if isinstance(distance, (int, float)) else "POI on")
        errors = [n for n, i in health.items() if isinstance(i, dict) and i.get("state") in ("dead", "stale")]
        if errors:
            parts.append("ERR " + ",".join(errors[:3]) + ("..." if len(errors) > 3 else ""))
    else:
        parts.append("STOPPED")
    parts.append(_sync_token())
    parts.append(f"{shutil.disk_usage('/').free / 1024**3:.0f}GB")
    temp = pctrl.read_cpu_temp()
    if temp is not None:
        parts.append(f"{temp:.0f}C")
    return bool(active), poi, " | ".join(parts)[:RC_TEXT_MAX]


# commands
def _rc_send(sock, lock, lines) -> None:
    data = "".join(line.replace("\n", " ") + "\n" for line in lines).encode("utf-8")
    with lock:
        sock.sendall(data)


def _rc_command(sock, lock, line: str) -> None:
    """Queue one 'CMD ...' line from psdk-bridge for the worker, which sends the reply back to the RC."""
    if not line.startswith("CMD "):
        return
    words = line[4:].split()
    if not words:
        return
    logger.info(f"RC command: {' '.join(words)}")

    def run():
        action = words[0]
        if action == "start":
            reply = pctrl.porter_start(words[1] if len(words) > 1 else "default")
        elif action == "stop":
            reply = pctrl.porter_stop()
        elif action == "shutdown":
            logger.warning("Shutdown command received from the RC - shutting down in 3 s")
            reply = "ACK:shutdown in 3 s"
            threading.Timer(3.0, lambda: subprocess.run(["sudo", "shutdown", "-h", "now"])).start()
        elif action == "poi" and len(words) > 1 and words[1] in ("on", "off"):
            name = "gimbal.starttrack" if words[1] == "on" else "gimbal.stoptrack"
            try:
                resp = pctrl.send_command(name)
                reply = f"ACK:{name}" if resp.get("ok") else f"ERR:{name} {resp.get('error', 'unknown error')}"
            except (ConnectionRefusedError, FileNotFoundError):
                reply = f"ERR:{name}: porter not running"
            except Exception as e:
                reply = f"ERR:{name}: {e}"
        else:
            reply = f"ERR:unknown RC command '{' '.join(words)}'"
        logger.info(f"RC reply: {reply}")
        try:
            running, poi, _ = _rc_status()
            _rc_send(sock, lock, [f"MSG {reply}", f"STATE {int(running)} {int(poi)}"])
        except OSError:
            pass                                               # psdk-bridge gone: the link thread reconnects

    _commands.put(run)


def _worker() -> None:
    """Runs RC commands one at a time, so the link thread keeps the status line going."""
    logger.info("Command worker started")
    while not _shutdown.is_set():
        try:
            job = _commands.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            job()
        except Exception:
            logger.exception("RC command failed")
    logger.info("Command worker stopped")


# link
def _rc_link() -> None:
    """Connect to psdk-bridge's control socket and keep the RC up to date.
    Without psdk-bridge (drone off, link lost) it just waits and retries."""
    logger.info("RC link thread started")
    waiting_logged = False
    while not _shutdown.is_set():
        if not os.path.exists(PSDK_CONTROL_SOCKET):
            if not waiting_logged:
                logger.info(f"RC link: waiting for psdk-bridge ({PSDK_CONTROL_SOCKET})")
                waiting_logged = True
            _shutdown.wait(RC_RETRY_PERIOD)
            continue
        lock = threading.Lock()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(PSDK_CONTROL_SOCKET)
                sock.settimeout(0.5)
                logger.info("RC link: connected to psdk-bridge")
                waiting_logged = False
                buf, next_status = b"", 0.0
                while not _shutdown.is_set():
                    if time.monotonic() >= next_status:
                        running, poi, text = _rc_status()
                        _rc_send(sock, lock, [f"STATE {int(running)} {int(poi)}", f"TEXT {text}"])
                        next_status = time.monotonic() + RC_STATUS_PERIOD
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        raise ConnectionError("psdk-bridge closed the control socket")
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        _rc_command(sock, lock, line.decode("utf-8", errors="replace").strip())
        except (OSError, ConnectionError) as e:
            logger.info(f"RC link: {e}; retrying in {RC_RETRY_PERIOD:.0f} s")
        except Exception as e:
            logger.exception(f"RC link error: {e}")
        _shutdown.wait(RC_RETRY_PERIOD)
    logger.info("RC link thread stopped")


# main
def main() -> None:
    def _on_signal(signum, frame):
        logger.info(f"Caught {signal.strsignal(signum)}, shutting down")
        _shutdown.set()

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(f"rcd starting, control socket {PSDK_CONTROL_SOCKET}")

    threads = [
        threading.Thread(target=_chrony_monitor, daemon=True, name="chrony"),   # first: the first status line has the sync state
        threading.Thread(target=_worker,         daemon=True, name="worker"),
        threading.Thread(target=_rc_link,        daemon=True, name="rc"),
    ]
    for t in threads:
        t.start()

    # watch the threads: if one dies, exit with an error so systemd restarts rcd
    while not _shutdown.wait(2.0):
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            logger.critical(f"rcd thread(s) died: {dead}")
            _crashed.set()
            _shutdown.set()

    logger.info("rcd stopped")
    sys.exit(1 if _crashed.is_set() else 0)


if __name__ == "__main__":
    main()