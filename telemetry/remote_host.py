#!/usr/bin/env python3
"""
remote_host.py  -  ground-station TUI.

Run on the laptop.  Shows live telemetry from porter (via telemetry packets) in the
upper panel, and a command prompt in the lower panel.

Usage:
  python3 remote_host.py [--port /dev/ttyUSB0] [--baudrate 38400]

Commands:
  ping              payload replies "pong" with RSSI
  reboot            reboot the payload computer
  shutdown          shut down the payload computer
  getlog            download last 40KiB of flight.log (base64-encoded)
  setconfig <path>  upload config file to payload
  start             start control.py on the payload
  stop              stop control.py on the payload
  camera.start      start a camera thread (Alvium only)
  camera.capture [-e <exposure>] [-g <gain>]  capture a single frame from the Alvium camera
  gimbal.goto <yaw> <pitch> <roll>  move gimbal to specified angles (degrees)
  gimbal.mode <mode>  set gimbal mode (off, lock, or follow)
  gimbal.starttrack   start pointing controller POI tracking (if configured)
  gimbal.stoptrack    stop pointing controller POI tracking
  position.mode <mode>  set position source (auto, payload or uav)
  $<cmd>            run shell command on payload, e.g. $ls -la
  jobs              list running background jobs
  canceljob <jid>   cancel a running background job
  help              list commands
  quit / q          exit
"""

import argparse
import base64
import curses
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from digi.xbee.exception import TimeoutException
from Xbee import Xbee, END_OF_MESSAGE_BYTE

# configuration
DEFAULT_PORT     = "/dev/ttyUSB0"
DEFAULT_BAUDRATE = 38400
PAYLOAD_NAME     = "LUKE"
READ_TIMEOUT     = 0.2

COMMANDS = {
    "ping":      "Check link (replies pong + RSSI)",
    "reboot":    "Reboot payload computer",
    "shutdown":  "Shut down payload computer",
    "start":     "Start control.py on payload",
    "stop":      "Stop control.py on payload",
    "getlog":    "Download last 40KiB of flight.log",
    "setconfig": "Upload config file  →  setconfig <local_path>",
    "camera.start": "Start a camera thread (Alvium only)",
    "camera.capture": "Capture frame  e.g. camera.capture -e 10000 -g 30.0 - very slow data transfer",
    "gimbal.goto": "Move gimbal to specified y, p, r angles (degrees)",
    "gimbal.starttrack": "Start pointing controller POI tracking (if configured)",
    "gimbal.stoptrack": "Stop pointing controller POI tracking",
    "gimbal.mode": "Set gimbal mode (off, lock, or follow)",
    "position.mode": "Set position source (auto, payload or uav)",
    "$<cmd>":    "Run shell command on payload  e.g. $ls -la",
    "jobs":       "List running background jobs",
    "canceljob":  "Cancel a running background job",
}

# shared state
_shutdown  = threading.Event()
_outbound: "queue.Queue[str]" = queue.Queue()

_state_lock = threading.Lock()
_tel        = {}          # latest parsed telemetry payload
_rssi       = None        # downlink RSSI (dBm)
_log        = []          # recent message log
LOG_MAX     = 40
_last_tel = {"sid": None, "seq": -1, "rx": None}   # newest telemetry shown; rx = local monotonic time



def _push_log(msg: str) -> None:
    with _state_lock:
        _log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(_log) > LOG_MAX:
            _log.pop(0)


# XBee I/O thread
def _io_thread(antenna: Xbee) -> None:
    global _rssi
    rx_buf = b""
    last_frame_time = 0.0


    while not _shutdown.is_set():

        while True:
            try:
                msg = _outbound.get_nowait()
            except queue.Empty:
                break
            try:
                antenna.send_msg(msg)
                display = msg if len(msg) <= 60 else msg[:60] + "…"
                _push_log(f"→ {display}")
            except Exception as e:
                _push_log(f"[send error] {e}")

        try:
            frame = antenna.device.read_data(timeout=READ_TIMEOUT)
        except TimeoutException:
            frame = None
        except Exception as e:
            if not _shutdown.is_set():
                _push_log(f"[read error] {e}")
            frame = None

        if frame is not None:
            now = time.monotonic()
            if rx_buf and now - last_frame_time > 1.0:
                _push_log("[discarding incomplete message after RX gap]")
                rx_buf = b""
            last_frame_time = now
            rx_buf += frame.data

            if rx_buf.endswith(END_OF_MESSAGE_BYTE):
                _on_message(rx_buf[:-1], antenna)
                rx_buf = b""
            elif len(rx_buf) > 80 * 1024:
                _push_log("[rx overflow, discarding]")
                rx_buf = b""


def _on_message(raw: bytes, antenna: Xbee) -> None:
    global _rssi
    text = raw.decode("utf-8", errors="replace").strip()

    if text.startswith("TEL:"):
        try:
            payload = json.loads(text[4:])
            sid, seq = payload.get("sid"), payload.get("seq", 0)
            with _state_lock:
                if sid == _last_tel["sid"] and seq <= _last_tel["seq"]:
                    return                      # older than what is already shown
                _last_tel.update(sid=sid, seq=seq, rx=time.monotonic())

            # read downlink RSSI from the XBee hardware register
            try:
                db = antenna.device.get_parameter("DB")
                with _state_lock:
                    _rssi = -int.from_bytes(db, byteorder="big")
            except Exception:
                pass
            with _state_lock:
                _tel.clear()
                _tel.update(payload)
        except json.JSONDecodeError as e:
            _push_log(f"[Telemetry parse error] {e}")

    elif text.startswith("LOGDATA:"):
        try:
            data = base64.b64decode(text[8:])
            fname = f"flight.log"
            with open(fname, "wb") as f:
                f.write(data)
            _push_log(f"← log saved → {fname} ({len(data)} bytes)")
        except Exception as e:
            _push_log(f"[LOGDATA decode error] {e}")

    elif text.startswith("CMDOUT:"):
        try:
            output = base64.b64decode(text[7:]).decode("utf-8", errors="replace")
            lines = output.splitlines()
            if not lines:
                lines = ["(empty output)"]
            for line in lines:
                _push_log(f"  {line}")
        except Exception as e:
            _push_log(f"[CMDOUT decode error] {e}")

    elif text.startswith("IMGDATA:"):
        try:
            img_bytes = base64.b64decode(text[8:])
            fname = f"captured_frame.jpg"
            with open(fname, "wb") as f:
                f.write(img_bytes)
            _push_log(f"← image saved → {fname} ({len(img_bytes) / 1024:.1f} KB)")
        except Exception as e:
            _push_log(f"[IMGDATA decode error] {e}")

    else:
        _push_log(f"← {text}")


# curses TUI
def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN,  -1)   # ok
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # stale
    curses.init_pair(3, curses.COLOR_RED,    -1)   # dead / error
    curses.init_pair(4, curses.COLOR_CYAN,   -1)   # header / label
    curses.init_pair(5, curses.COLOR_WHITE,  -1)   # normal


C_OK     = lambda: curses.color_pair(1) | curses.A_BOLD
C_STALE  = lambda: curses.color_pair(2) | curses.A_BOLD
C_DEAD   = lambda: curses.color_pair(3) | curses.A_BOLD
C_HEADER = lambda: curses.color_pair(4) | curses.A_BOLD
C_NORMAL = lambda: curses.color_pair(5)
C_DIM    = lambda: curses.color_pair(5) | curses.A_DIM


def _state_attr(state: str) -> int:
    if state == "ok":    return C_OK()
    if state == "stale": return C_STALE()
    return C_DEAD()


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> int:
    """Write text clipped to the window width. Returns next y."""
    h, w = win.getmaxyx()
    if y >= h - 1:
        return y
    text = text[: w - x - 1]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass
    return y + 1


def _draw(stdscr, input_buf: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    with _state_lock:
        tel  = dict(_tel)
        rssi = _rssi
        log  = list(_log)
        tel_rx = _last_tel["rx"]

    row = 0

    # header 
    title = " PORTER GROUND STATION "
    row = _safe_addstr(stdscr, row, 0, title.center(w, "═"), C_HEADER())

    system = tel.get("system", {})
    if system:
        t     = system.get("time")
        tstr  = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"
        free  = system.get("hddfree", 0)
        total = system.get("hddtot", 1)
        pct   = free / total * 100 if total else 0
        cpu_t = system.get("cputemp")
        line  = f"Time: {tstr}   Disk: {free:.1f}/{total:.1f} GiB ({pct:.0f}%)   CPU: "
        row = _safe_addstr(stdscr, row, 2, line, C_NORMAL())
        row -= 1  # stay on same row
        if cpu_t is None:
            temp_str, temp_attr = "-", C_NORMAL()
        else:
            temp_str  = f"{cpu_t:.1f} °C"
            temp_attr = C_OK() if cpu_t < 70 else (C_STALE() if cpu_t < 80 else C_DEAD())
        _safe_addstr(stdscr, row, 2 + len(line), temp_str, temp_attr)
        row += 1

    rssi_str = f"{rssi} dBm" if rssi is not None else "-"
    porter   = tel.get("porter", "-")
    porter_attr = C_OK() if porter.startswith("running") else (C_STALE() if porter == "stopped" else C_DEAD())
    row = _safe_addstr(stdscr, row, 2, f"Downlink RSSI: {rssi_str}   Porter: ", C_NORMAL())
    row -= 1  # stay on same row
    x_porter = 2 + len(f"Downlink RSSI: {rssi_str}   Porter: ")
    _safe_addstr(stdscr, row, x_porter, porter.upper(), porter_attr)
    row += 1

    if tel_rx is None:
        age_str, age_attr = "no data", C_DEAD()
    else:
        age = time.monotonic() - tel_rx
        age_str = f"{age:.0f} s"
        age_attr = C_OK() if age < 10 else (C_STALE() if age < 30 else C_DEAD())
    row = _safe_addstr(stdscr, row, 2, "Telemetry age: ", C_NORMAL())
    row -= 1
    _safe_addstr(stdscr, row, 2 + len("Telemetry age: "), age_str, age_attr)
    row += 1
    
    disk = tel.get("disk", {})
    if disk:
        lvl = disk.get("level", "?")
        lvl_attr = {"ok": C_OK(), "warning": C_STALE()}.get(lvl, C_DEAD())
        left = disk.get("left_min")
        left_str = f"~{left:.0f} min left" if left is not None else "no data growth"
        line = "Disk guard: "
        row = _safe_addstr(stdscr, row, 2, line, C_NORMAL())
        row -= 1
        _safe_addstr(stdscr, row, 2 + len(line),
                     f"{lvl.upper()}  {disk.get('free_gb', 0):.1f} GB free, "
                     f"{disk.get('rate_mbs', 0):.1f} MB/s, {left_str}", lvl_attr)
        row += 1


    # chrony status
    chrony = tel.get("chrony", {})
    chrony_state = chrony.get("ok", False)
    chrony_ref = chrony.get("ref", "")
    chrony_attr = C_OK() if chrony_state else C_DEAD()
    row = _safe_addstr(stdscr, row, 2, f"Chrony: ", C_NORMAL())
    row -= 1
    x_chrony = 2 + len(f"Chrony: ")
    _safe_addstr(stdscr, row, x_chrony, "OK" if chrony_state else "ERR", chrony_attr)
    if chrony_ref:
        row = _safe_addstr(stdscr, row, x_chrony + 4, f"Time reference: {chrony_ref}", C_NORMAL())
    row += 1

    # gimbal and UAV link status (only shown if configured, i.e. present in the telemetry)
    health = tel.get("health", {})
    meta = tel.get("meta", {})
    links = []
    for name, ok_word in (("Gimbal", "CONNECTED"), ("UAV", "RECEIVING")):
        if name in health:
            state = health[name].get("state", "?")
            if state == "ok":
                links.append((f"{name}: {ok_word}", C_OK()))
            else:
                reason = meta.get(name, {}).get("error", state)
                links.append((f"{name}: {reason.upper()}", C_DEAD() if state == "dead" else C_STALE()))
    if links:
        x = 2
        for text, attr in links:
            _safe_addstr(stdscr, row, x, text, attr)
            x += len(text) + 4
        row += 1

    # power controller status
    powerd = tel.get("power_controller", {})
    powerd_state = powerd.get("ok", False)
    powerd_bus_voltage = powerd.get("reading", {}).get("bus_voltage", None)
    powerd_shunt_voltage = powerd.get("reading", {}).get("shunt_voltage", None)
    powerd_current = powerd.get("reading", {}).get("current", None)
    powerd_temperature = powerd.get("reading", {}).get("temperature", None)
    powerd_attr = C_OK() if powerd_state else C_DEAD()
    row = _safe_addstr(stdscr, row, 2, f"Power Controller: ", C_NORMAL())
    row -= 1
    x_powerd = 2 + len(f"Power Controller: ")
    _safe_addstr(stdscr, row, x_powerd, "OK" if powerd_state else "ERR", powerd_attr)
    if powerd_bus_voltage is not None:
        row = _safe_addstr(stdscr, row, x_powerd + 4, f"Bus Voltage: {powerd_bus_voltage:.2f} V", C_NORMAL())
    if powerd_shunt_voltage is not None:
        row = _safe_addstr(stdscr, row, x_powerd + 4, f"Shunt Voltage: {powerd_shunt_voltage:.2f} V", C_NORMAL())
    if powerd_current is not None:
        row = _safe_addstr(stdscr, row, x_powerd + 4, f"Current: {powerd_current:.2f} A", C_NORMAL())
    if powerd_temperature is not None:
        row = _safe_addstr(stdscr, row, x_powerd + 4, f"Temperature: {powerd_temperature:.2f} °C", C_NORMAL())
    row += 1

    # health
    row = _safe_addstr(stdscr, row, 0, "─" * w, C_DIM())
    row = _safe_addstr(stdscr, row, 2, "HEALTH", C_HEADER())

    health = tel.get("health", {})
    meta   = tel.get("meta", {})
    for name, info in health.items():
        if row >= h - 6:
            break
        state = info.get("state", "?") if isinstance(info, dict) else str(info)
        age   = info.get("age_s", "") if isinstance(info, dict) else ""
        age_s = f"  {age}s" if age != "" else ""
        m     = meta.get(name, {})
        meta_s = "  " + "  ".join(f"{k}={v}" for k, v in m.items()) if m else ""
        label = f"  {name:<20}"
        row = _safe_addstr(stdscr, row, 0, label, C_NORMAL())
        row -= 1
        _safe_addstr(stdscr, row, len(label), f"[{state}]", _state_attr(state))
        rest = f"{age_s}{meta_s}"
        _safe_addstr(stdscr, row, len(label) + len(f"[{state}]"), rest, C_DIM())
        row += 1

    # log
    row = _safe_addstr(stdscr, row, 0, "─" * w, C_DIM())
    row = _safe_addstr(stdscr, row, 2, "LOG", C_HEADER())

    log_rows = h - row - 3   # leave 3 rows for prompt
    visible  = log[-log_rows:] if log_rows > 0 else []
    for line in visible:
        attr = C_OK() if "← pong" in line or "← ACK" in line else \
               C_DEAD() if "← ERR" in line or "[error]" in line else C_NORMAL()
        row = _safe_addstr(stdscr, row, 2, line, attr)

    # command prompt
    prompt_row = h - 2
    _safe_addstr(stdscr, prompt_row - 1, 0, "─" * w, C_DIM())
    _safe_addstr(stdscr, prompt_row, 0,
                 f"  cmd> {input_buf}", C_NORMAL())

    stdscr.move(prompt_row, 7 + len(input_buf))
    stdscr.refresh()


def _tui(stdscr, antenna: Xbee) -> None:
    _init_colors()
    curses.curs_set(1)
    stdscr.keypad(True)
    stdscr.timeout(300)   # non-blocking getch with 300 ms timeout

    input_buf = ""

    while not _shutdown.is_set():
        _draw(stdscr, input_buf)

        ch = stdscr.getch()
        if ch == -1:
            continue

        if ch in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            original_input = input_buf.strip()
            cmd = original_input.lower()
            input_buf = ""
            if cmd in ("quit", "exit", "q"):
                _shutdown.set()
                break
            elif cmd == "help":
                _push_log("Available commands:")
                for c, desc in COMMANDS.items():
                    _push_log(f"  {c:<12} {desc}")
            elif original_input.startswith("$"):
                _outbound.put(original_input)
            elif original_input.startswith("gimbal.goto"):
                _outbound.put(original_input)
            elif original_input.startswith("gimbal.mode"):
                _outbound.put(original_input)
            elif original_input.startswith("position.mode"):
                _outbound.put(original_input)
            elif original_input.startswith("gimbal.starttrack"):
                _outbound.put(original_input)
            elif original_input.startswith("gimbal.stoptrack"):
                _outbound.put(original_input)
            elif cmd.startswith("camera.start"):
                _outbound.put(original_input)
            elif cmd.startswith("camera.capture"):
                _outbound.put(original_input)
            elif cmd.startswith("setconfig"):
                parts = original_input.split(None, 1)  # preserve original case for path
                if len(parts) < 2:
                    _push_log("Usage: setconfig <local_path>")
                else:
                    path = parts[1].strip()
                    try:
                        with open(path, "rb") as fh:
                            raw_yml = fh.read()
                        encoded = base64.b64encode(raw_yml).decode("ascii")
                        _outbound.put(f"setconfig:{encoded}")
                        _push_log(f"→ setconfig ({len(raw_yml)} bytes from {path})")
                    except FileNotFoundError:
                        _push_log(f"ERR: file not found: {path}")
                    except Exception as e:
                        _push_log(f"ERR: {e}")
            elif cmd.startswith("canceljob"):
                parts = original_input.split(None, 1)
                if len(parts) < 2:
                    _push_log("Usage: canceljob <jid>")
                else:
                    jid = parts[1].strip()
                    _outbound.put(f"canceljob {jid}")
            elif cmd == "start" or cmd.startswith("start "):
                _outbound.put(original_input.strip())
            elif cmd in COMMANDS:
                _outbound.put(cmd)
            elif cmd:
                _push_log(f"Unknown command '{cmd}'. Type 'help'.")

        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            input_buf = input_buf[:-1]

        elif 32 <= ch < 127:
            input_buf += chr(ch)


# main
def main() -> None:
    parser = argparse.ArgumentParser(description="Porter ground station")
    parser.add_argument("--port",     default=DEFAULT_PORT,    help="Local XBee serial port")
    parser.add_argument("--baudrate", default=DEFAULT_BAUDRATE, type=int)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        print("Skipping connection to XBee for debug mode. Use --debug only for testing the TUI.")
        antenna = None 

    else:
        print(f"Connecting to {args.port} @ {args.baudrate} baud ...")
        try:
            antenna = Xbee(port=args.port, baudrate=args.baudrate)
            antenna.open(force_settings=True, remote_name=PAYLOAD_NAME)
        except Exception as e:
            print(f"Failed to open XBee: {e}")
            sys.exit(1)
        print(f"Connected. Remote: {antenna.remote_device}\nStarting TUI...")
        time.sleep(0.5)

        t = threading.Thread(target=_io_thread, args=(antenna,), daemon=True)
        t.start()

    try:
        curses.wrapper(_tui, antenna)
    finally:
        _shutdown.set()
        print("Closing XBee...")
        try:
            antenna.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()