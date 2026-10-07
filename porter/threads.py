import json
import os
import copy
import logging
import threading
import time
import subprocess
import signal
import shutil
from collections import deque
from porter.PositionSource import PositionSource

from porter.process_utils import start_process, stop_process

logger = logging.getLogger(__name__)

LINK_RETRY_S      = 10.0   # retry a failed gimbal connection / UAV port every 10 s
GIMBAL_SILENT_S   = 3.0    # connected gimbal with no message for this long -> "lost"

try:
    from sour_core import sony
    logger.info("Sony Camera module imported successfully")
except ModuleNotFoundError:
    logger.info("Sony Camera module not found")
except Exception as e:
    logger.error(f"Error importing Sony Camera module: {e}")
    pass

try:
    import pyalvium
    logger.info("Alvium Camera module imported successfully")
except ModuleNotFoundError:
    logger.info("Alvium Camera module not found")
except Exception as e:
    logger.error(f"Error importing Alvium Camera module: {e}")
    pass

try:
    from lager import PointingController as PC
    logger.info("Lager module imported successfully")
except ModuleNotFoundError:
    logger.info("Lager module not found")
except Exception as e:
    logger.error(f"Error importing Lager module: {e}")
    pass


class Sensors(threading.Thread):

    def __init__(
        self,
        handler,
        flag,
        date,
        path,
        status_board,
        sensor_name=None,
        *args,
        **kwargs,
    ):
        """Class to create a thread for each sensor

        Parameters:
            conn (Object): object with the connection to a specific sensor
            flag (threading.Event): flag to communicate to the thread a
                                    particular event happened
            date (str): string with the date and time at the program start
            path (str): path for file storage
            status_board (StatusBoard): board to track sensor status
        """

        super().__init__(*args, **kwargs)

        self.sensor_name = sensor_name
        self.status_board = status_board
        self.status_board.register(self.sensor_name)


        self.datafile_name = os.path.join(path, f"{self.sensor_name}_{date}.bin") if self.sensor_name else os.path.join(path, f"sensor_{date}.bin")
        self.shutdown_flag = flag
        self.handler = handler
        # set once _connection()/_configuration() have been attempted (success or failure),
        # so other threads can wait for this sensor to be ready without polling
        self.ready = threading.Event()

    def run(self):
        try:
            self.handler._connection()
            self.handler._configuration()
        except Exception as e:
            logger.error(f"Sensor {self.sensor_name} failed to initialize, skipping: {e}")
            self.status_board.mark_failed(self.sensor_name, f"init failed: {e}")
            self.ready.set()
            return
        self.ready.set()

        logger.info(f"Sensor {self.sensor_name} started")
        try:
            self.handler.obj.read_continous_binary(self.shutdown_flag, self.datafile_name, self.status_board)
        except Exception as e:
            logger.exception(f"Sensor {self.sensor_name} crashed")
            self.status_board.mark_failed(self.sensor_name, f"crashed: {e}")
        finally:
            close = getattr(self.handler.obj, "close", None)
            if close is not None:
                try:
                    close()          # safe to call twice
                except Exception:
                    logger.exception(f"Sensor {self.sensor_name}: close() failed")
        logger.info(f"Sensor {self.sensor_name} closed")


class AlviumCameraStarspec(threading.Thread):

    def __init__(
        self,
        camera_config,
        path,
        flag,
        status_board,
        *args,
        **kwargs,
    ):
        '''
        Class to create a thread for the camera

        Parameters:
            camera (Object): camera object
            flag (threading.Event): flag to communicate to the thread a particular event happened
            camera_mode (str): camera mode
            fps (float): number of fps in case of photo mode
            status_board (StatusBoard): board to track camera health
        '''
        super().__init__(*args, **kwargs)

        self.camera_config = camera_config
        self.path = path

        self.camera_name = self.camera_config["name"]
        self.shutdown_flag = flag
        self.status_board = status_board
        self.process = None

    def run(self):
        self.frame_rate = self.camera_config.get("frame_rate", 5)
        self.mode = self.camera_config.get("mode", 'trigger')
        self.core = self.camera_config.get("core", None)
        self.verbosity = self.camera_config.get("verbosity", False)
        self.roi = self.camera_config.get("roi", None)
        self.processing = self.camera_config.get("processing", False)
        self.exposure = self.camera_config.get("exposure", None)
        self.output = self.path

        cmd = f"alvium --framerate {self.frame_rate} --mode {self.mode}"
        if self.verbosity == True:
            cmd += f" --debug"
        if self.processing == True:
            cmd += f" --processing"
        if self.exposure is not None:
            cmd += f" --exposure {self.exposure}"
        if self.roi is not None:
            cmd += f" --roi {str(self.roi)}"
        if self.output is not None:
            cmd += f" --output {self.output}"
        if self.core is not None:
            cmd += f" --core {int(self.core)}"
        log_path = (self.output.rstrip("/") if self.output else os.path.join(self.path, self.camera_name)) + "_stdout.log"
        with open(log_path, "ab") as log:
            self.process = start_process(cmd, stdout=log, stderr=subprocess.STDOUT)
        launch_time = time.monotonic()

        while not self.shutdown_flag.is_set():
            self.shutdown_flag.wait(1)
            if self.process is not None and self.process.poll() is not None:
                logger.error(f"{self.camera_name} process exited unexpectedly (exit code {self.process.returncode})")
                break
            # After startup grace period, check that the output directory is receiving new files
            if time.monotonic() - launch_time > 10.0:
                try:
                    if time.time() - os.path.getmtime(self.output) > 5.0:
                        logger.error(f"{self.camera_name}: no new data written for >5s, camera may be disconnected")
                        break
                except OSError:
                    pass
            self.status_board.beat(self.camera_name)

        self.close()

    def close(self):
        stop_process(self.process, self.name, first_signal=signal.SIGINT, grace=5)
        self.process = None
        logger.info(f"Closed sensor {self.name}")

class AlviumCamera(threading.Thread):
    def __init__(
        self,
        camera_config,
        path,
        flag,
        status_board,
        *args,
        **kwargs,
    ):
        '''
        Class to create a thread for the camera

        Parameters:
            camera (Object): camera object
            flag (threading.Event): flag to communicate to the thread a particular event happened
            camera_mode (str): camera mode
            fps (float): number of fps in case of photo mode
            status_board (StatusBoard): board to track camera health
        '''
        super().__init__(*args, **kwargs)

        self.camera_config = camera_config
        self.path = path

        self.camera_name = self.camera_config["name"]
        self.shutdown_flag = flag
        self.status_board = status_board

        self.core = self.camera_config.get("core", None)
        self.exposure = self.camera_config.get("exposure", None)
        self.gain = self.camera_config.get("gain", None)
        self.format = self.camera_config.get("format", None)
        self.max_framerate = self.camera_config.get("max_framerate", None)
        self.writing_threads = self.camera_config.get("writing_threads", None)
        self.verbosity = self.camera_config.get("verbosity", None)
        self.output = self.path

        self.settings = {
            "exposure": self.exposure,
            "gain": self.gain,
            "format": self.format,
            "max_framerate": self.max_framerate,
        }

    def run(self):
        with pyalvium.Camera(output_path=self.output, writing_threads=self.writing_threads, settings=self.settings, verbose=self.verbosity) as camera:
            camera.log_all_features()
            camera.start_acquisition()

            while not self.shutdown_flag.is_set():
                self.shutdown_flag.wait(1)
                # read live frame count directly from the folder
                frame_count = len([f for f in os.listdir(self.output) if f.endswith(".raw")])
                self.status_board.beat(self.camera_name, {"frames_captured": frame_count})

            camera.stop_acquisition()
            logger.info(f"Closed sensor {self.name}")
            self.stats = camera.get_streaming_stats()

class SonyCamera(threading.Thread):
    def __init__(
        self,
        camera_config,
        flag,
        *args,
        **kwargs,
    ):
        """Class to create a thread for each sensor

        Parameters:
            camera (Object): camera object
            flag (threading.Event): flag to communicate to the thread a
                                    particular  event happened
            camera_mode (str): camera mode
            fps (float): number of fps in case of photo mode
            frames (int): number of photo in case of photo mode
            duration (float): duration of the video in case of video mode
        """

        super().__init__(*args, **kwargs)

        self.camera_config = camera_config
        self.camera_name = self.camera_config["name"]
        self.shutdown_flag = flag

    def run(self):
        try:
            camera = sony.SONYconn(self.camera_name, log=logger)
        except IndexError:
            self.shutdown_flag.set()
            logger.info("Camera not Found, stopping the code")
        
        if not self.shutdown_flag.is_set():
            camera.initialize_camera()

            time.sleep(0.2)
            camera.messageHandler(["datetime", 0.04, 1e-3])
            time.sleep(0.1)
            camera.messageHandler(["programmode", self.camera_config["program"]])
            time.sleep(0.1)

            if "ISO" in self.camera_config.keys():
                camera.messageHandler(["iso", self.camera_config["ISO"]])
                time.sleep(0.1)

            if "shutter_speed" in self.camera_config.keys():
                camera.messageHandler(["shutterspeed", self.camera_config["shutter_speed"]])
                time.sleep(0.1)

            if "focus_distance" in self.camera_config.keys():
                camera.messageHandler(
                    ["focusdistance", self.camera_config["focus_distance"]]
                )
                time.sleep(0.1)

            logger.info(f"Camera {self.camera_name} Configured")

        if self.camera_config["mode"] == "video":
            if "duration" in self.camera_config.keys():
                duration = self.camera_config["duration"]
            else:
                duration = 20 * 60

            recording = True
            video_chunks = 30 * 60
            secs_remaining = copy.copy(duration)
            while not self.shutdown_flag.is_set():
                self.shutdown_flag.wait(1)
                camera.messageHandler(["videocontrol"])
                if recording:
                    if secs_remaining < video_chunks:
                        logger.info(
                            f"Camera {self.camera_name} starts recording, remaining {secs_remaining} s"
                        )
                        self.shutdown_flag.wait(secs_remaining)
                        camera.messageHandler(["videocontrol"])
                        self.shutdown_flag.set()
                        recording = not recording
                        logger.info(f"Camera {self.camera_name} stops recording")
                        break
                    else:
                        self.shutdown_flag.wait(video_chunks)
                        camera.messageHandler(["videocontrol"])
                        time.sleep(2)
                        secs_remaining -= video_chunks
                else:
                    recording = not recording

        elif self.camera_config["mode"] == "photo":
            if "fps" in self.camera_config.keys():
                fps = self.camera_config["fps"]
            else:
                fps = 1

            if "frames" in self.camera_config.keys():
                frames = self.camera_config["frames"]
            else:
                frames = 1e9

            timing = 1/fps

            photo_count = 0
            while not self.shutdown_flag.is_set():
                t = time.time()
                camera.messageHandler(["capture"])

                while (time.time() - t) < timing:
                    self.shutdown_flag.wait(0.1)

                photo_count += 1
                if photo_count > frames:
                    break

        logger.info(f"Camera {self.camera_name} stopped")
        try:
            camera.close_usb_connection()
        except UnboundLocalError:
            pass

class PointingController(threading.Thread):
    """Class to create a thread for the pointing controller"""
    def __init__(
        self,
        pointing_controller_config,
        path,
        flag,
        status_board,
        gnss_source=None,
        position_config=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.pointing_controller_config = pointing_controller_config
        self.name = self.pointing_controller_config["name"]
        self.path = path
        self.status_board = status_board
        self.gnss_source = gnss_source
        self.pointing_controller_name = self.pointing_controller_config["name"]
        self.shutdown_flag = flag
        self.pc = None
        self.start_track_flag = False
        self.start_track_flag_old = False
        self.position_config = position_config or {}
        self.position = None
        self.gimbal_link = "connecting"     # connecting | connected | not connected | lost
        self.uav_link = "connecting"        # connecting | port error | waiting for data | receiving | silent
        self._gimbal_ok = False             # connect() succeeded
        self._uav_port_ok = False           # serial port open, telemetry reader running
        self._gimbal_retry_at = 0.0
        self._uav_retry_at = 0.0
        self._gimbal_connecting = False     # a connect thread is running for this link
        self._uav_connecting = False




    def run(self):
        # build lager's objects (no connection yet); a configuration error here is still fatal
        try:
            self.pc = PC(configuration=self.pointing_controller_config, data_folder=self.path)
        except Exception as e:
            logger.error(f"Error creating pointing controller (configuration?): {e}")
            self.shutdown_flag.set()
            return
        gimbal = getattr(self.pc, "gimbal", None)

        uav = getattr(self.pc, "uav", None)
        # only configured devices appear in the telemetry; both links open in the background
        if gimbal is not None:
            self.status_board.mark_failed("Gimbal", "connecting")
            self._start_link("gimbal", self._connect_gimbal)
        if uav is not None:
            self.status_board.mark_failed("UAV", "connecting")
            self._start_link("uav", self._connect_uav)
        self.position = PositionSource(payload_meta=self.gnss_source, uav=uav, config=self.position_config)

        while not self.shutdown_flag.is_set():
            self.shutdown_flag.wait(1)
            now = time.monotonic()

            # gimbal link
            if gimbal is not None:
                if not self._gimbal_ok and not self._gimbal_connecting and now >= self._gimbal_retry_at:
                    self._start_link("gimbal", self._connect_gimbal)
                if self._gimbal_ok:
                    age_fn = getattr(gimbal.telemetry_state, "age", None)
                    age = age_fn() if age_fn else 0.0
                    self.gimbal_link = "lost" if age > GIMBAL_SILENT_S else "connected"
                if self.gimbal_link == "connected":
                    data = gimbal.telemetry_state.get()
                    r2 = lambda v: round(v, 2) if v is not None else None
                    self.status_board.beat("Gimbal", {
                        "link":  "connected",
                        "yaw":   r2(data.get("yaw")),
                        "pitch": r2(data.get("pitch")),
                        "roll":  r2(data.get("roll")),
                        "mode":  getattr(gimbal, "mode", None),
                    })
                elif self.gimbal_link == "lost":
                    self.status_board.mark_failed("Gimbal", f"lost: no message for {age:.0f} s")
                elif self.gimbal_link == "connecting":
                    self.status_board.mark_failed("Gimbal", "connecting")
                else:
                    self.status_board.mark_failed("Gimbal", f"{self.gimbal_link} (retry in {LINK_RETRY_S:.0f} s)")

            # UAV link (passive: "connected" means packets are arriving)
            if uav is not None:
                if not self._uav_port_ok and not self._uav_connecting and now >= self._uav_retry_at:
                    self._start_link("uav", self._connect_uav)
                if self._uav_port_ok:
                    age = uav.telemetry_state.age()
                    if age == float("inf"):
                        self.uav_link = "waiting for data"
                    elif age > self.position.uav_max_age:
                        self.uav_link = f"silent {age:.0f} s"
                    else:
                        self.uav_link = "receiving"
                if self.uav_link == "receiving":
                    d = uav.telemetry_state.get()
                    r1 = lambda v: round(v, 1) if v is not None else None
                    self.status_board.beat("UAV", {
                        "link":    "receiving",
                        "yaw":     r1(d.get("yaw")),
                        "pitch":   r1(d.get("pitch")),
                        "roll":    r1(d.get("roll")),
                        "heading": r1(d.get("rtk_yaw_deg")),
                        "rtk":     d.get("rtk_pos_health"),
                    })
                elif self.uav_link == "waiting for data":
                    self.status_board.mark_failed("UAV", "waiting for data (passive link)")
                else:
                    self.status_board.mark_failed("UAV", self.uav_link)

            # POI tracking: only while the gimbal is connected
            poi = getattr(self.pc, "poi", None)
            if poi is not None:
                if self.start_track_flag and not self.start_track_flag_old and self.gimbal_link == "connected":
                    poi.start_tracking(gimbal=gimbal, gnss_source=self.position, forward_heading=True)
                    logger.info(f"Pointing controller {self.pointing_controller_name} started tracking POI")
                    self.start_track_flag_old = True
                elif not self.start_track_flag and self.start_track_flag_old:
                    poi.stop_tracking()
                    logger.info(f"Pointing controller {self.pointing_controller_name} stopped tracking POI")
                    self.start_track_flag_old = False
                data = poi.get_data()
                distance = data.get("current_distance")
                self.status_board.beat("POI", {"tracking": data.get("is_tracking"),
                                               "distance": round(distance, 2) if distance is not None else None})
            
            self.position.get_position()        # refresh the source choice even when not tracking
            self.status_board.beat("Position", self.position.status())


        logger.info(f"Stopping Pointing Controller {self.pointing_controller_name}")
        try:
            poi = getattr(self.pc, "poi", None)
            if poi is not None:
                poi.stop_tracking()
            if uav is not None and self._uav_port_ok:
                uav.stop_telemetry()
                uav.disconnect()
            if gimbal is not None and self._gimbal_ok:
                gimbal.stop_telemetry()
                gimbal.disconnect()
        except Exception as e:
            logger.error(f"Error stopping pointing controller: {e}")


    def start_tracking(self):
        self.start_track_flag = True
    
    def stop_tracking(self):
        self.start_track_flag = False

    def _connect_gimbal(self):
        g = self.pc.gimbal
        self.gimbal_link = "connecting"
        try:
            g.connect()
            g.start_telemetry()
            if self.shutdown_flag.is_set():          # finished after shutdown started: undo
                g.stop_telemetry()
                g.disconnect()
                return
            self._gimbal_ok = True
            self.gimbal_link = "connected"
            logger.info("Gimbal connected")
        except Exception as e:
            self._gimbal_ok = False
            self.gimbal_link = "not connected"
            self._gimbal_retry_at = time.monotonic() + LINK_RETRY_S
            logger.error(f"Gimbal not connected: {e} (retry in {LINK_RETRY_S:.0f} s)")
            try:
                g.disconnect()      # stop the heartbeat thread and close the port before the next try
            except Exception:
                pass

    def _connect_uav(self):
        u = self.pc.uav
        self.uav_link = "connecting"
        try:
            u.connect()             # passive link: the handshake times out after ~15 s, which is fine
            u.start_telemetry()
            if self.shutdown_flag.is_set():
                u.stop_telemetry()
                u.disconnect()
                return
            self._uav_port_ok = True
            self.uav_link = "waiting for data"
            logger.info("UAV serial port open, waiting for telemetry (passive link)")
        except Exception as e:
            self._uav_port_ok = False
            self.uav_link = "port error"
            self._uav_retry_at = time.monotonic() + LINK_RETRY_S
            logger.error(f"UAV port error: {e} (retry in {LINK_RETRY_S:.0f} s)")


    def _start_link(self, name, connect_fn):
        """Run connect_fn in a background thread, at most one at a time per link."""
        busy = f"_{name}_connecting"
        if getattr(self, busy):
            return
        setattr(self, busy, True)
        def _run():
            try:
                connect_fn()
            finally:
                setattr(self, busy, False)
        threading.Thread(target=_run, name=f"{name}-connect", daemon=True).start()


class StatusWriter(threading.Thread):
    """Write the status board snapshot to a JSON file for telemd to read.

    Replaces the Telemetry thread inside control.py.  telemd.py reads the file
    and forwards the payload over the XBee link, keeping radio ownership
    entirely outside the flight software.

    Parameters
    ----------
    status_board : StatusBoard
        Shared status board populated by all sensor threads.
    update_rate : float
        How many times per second to refresh the status file.
    flag : threading.Event
        Shutdown flag; the thread exits when it is set.
    status_file : str
        Path of the JSON file to write (must match telemd STATUS_FILE).
    """

    STATUS_FILE = "/tmp/porter_status.json"

    def __init__(self, status_board, update_rate, flag, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_board = status_board
        self.update_rate  = update_rate
        self.shutdown_flag = flag

    def run(self):
        logger.info(f"StatusWriter started, writing to {self.STATUS_FILE} at {self.update_rate} Hz")
        while not self.shutdown_flag.is_set():
            self.shutdown_flag.wait(1.0 / self.update_rate)

            payload = {
                "health": self.status_board.get_health(),
                "meta":   self.status_board.get_metadata(),
                "disk":   self.status_board.get_disk(),
            }

            try:
                # write atomically via a temp file to avoid partial reads by telemd
                tmp = self.STATUS_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f, separators=(",", ":"))
                os.replace(tmp, self.STATUS_FILE)
            except OSError as e:
                logger.warning(f"StatusWriter: could not write status file: {e}")

        # remove the status file on clean shutdown so telemd knows control.py is done
        try:
            os.remove(self.STATUS_FILE)
        except FileNotFoundError:
            pass
        logger.info("StatusWriter stopped")

class DiskGuard(threading.Thread):
    """
    Watches free space on the filesystems porter writes to and stops the whole
    acquisition (clean shutdown) before any of them fills up.
    """

    def __init__(self, paths, status_board, shutdown_flag, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_board  = status_board
        self.shutdown_flag = shutdown_flag
        self.interval  = float(config.get("interval_s", 3))
        self.window    = float(config.get("window_s", 45))
        self.min_free  = float(config.get("min_free_gb", 0.8)) * 1024**3
        self.stop_s    = float(config.get("stop_margin_s", 30))
        self.warn_s    = float(config.get("warn_margin_s", 1200))
        self.triggered = threading.Event()        # set when DiskGuard stopped the run
        self.level     = "ok"

        # one entry per filesystem (st_dev), so a separate data disk is checked on its own
        self.filesystems = {}
        for p in paths:
            self.filesystems.setdefault(os.stat(p).st_dev, p)
        n = max(2, int(self.window / self.interval) + 1)
        self.history = {dev: deque(maxlen=n) for dev in self.filesystems}

    def run(self):
        logger.info(f"DiskGuard started, watching {list(self.filesystems.values())}")
        while not self.shutdown_flag.wait(self.interval):
            try:
                self._check()
            except Exception:
                logger.exception("DiskGuard check failed")
        logger.info("DiskGuard stopped")

    def _check(self):
        now = time.monotonic()
        worst = None
        for dev, path in self.filesystems.items():
            free = shutil.disk_usage(path).free          # space available to non-root users
            h = self.history[dev]
            h.append((now, free))
            t0, f0 = h[0]
            rate = max(0.0, (f0 - free) / (now - t0)) if now > t0 else 0.0   # bytes/s
            above_floor = free - self.min_free
            left_s = above_floor / rate if rate > 0 else float("inf")
            if worst is None or above_floor <= 0 or left_s < worst["left_s"]:
                worst = {"path": path, "free": free, "rate": rate,
                         "left_s": left_s, "above_floor": above_floor}

        if worst["above_floor"] <= 0 or worst["left_s"] <= self.stop_s:
            level = "stopped"
        elif worst["left_s"] <= self.warn_s:
            level = "warning"
        else:
            level = "ok"

        self.status_board.set_disk({
            "level":    level,
            "free_gb":  round(worst["free"] / 1024**3, 2),
            "rate_mbs": round(worst["rate"] / 1024**2, 2),
            "left_min": None if worst["left_s"] == float("inf") else round(worst["left_s"] / 60, 1),
            "path":     worst["path"],
        })

        if level != self.level:
            log = logger.critical if level == "stopped" else logger.warning
            log(f"DiskGuard: {self.level} -> {level} ({worst['free']/1024**3:.2f} GB free, "
                f"{worst['rate']/1024**2:.1f} MB/s on {worst['path']})")
            self.level = level

        if level == "stopped":
            self.triggered.set()
            self.shutdown_flag.set()           # clean shutdown of everything: camera and sensors
