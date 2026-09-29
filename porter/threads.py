import json
import os
import copy
import logging
import threading
import time
import subprocess
import signal
import shutil

from porter.process_utils import start_process, stop_process

logger = logging.getLogger(__name__)

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


    def run(self):
        try:
            self.pc = PC(configuration=self.pointing_controller_config, data_folder=self.path)
            self.pc.connect()
            self.pc.start_telemetry()
        except Exception as e:
            self.shutdown_flag.set()
            logger.error(f"Error initializing pointing controller: {e}")
            self.pc = None
            pass

        while not self.shutdown_flag.is_set():
            self.shutdown_flag.wait(1)

            if self.pc is not None:
                if self.pc.poi is not None:
                    if self.start_track_flag and not self.start_track_flag_old:
                        self.pc.poi.start_tracking(gimbal=self.pc.gimbal, gnss_source=self.gnss_source, forward_heading=True)
                        logger.info(f"Pointing controller {self.pointing_controller_name} started tracking POI")
                        self.start_track_flag_old = self.start_track_flag
                    elif not self.start_track_flag and self.start_track_flag_old:
                        self.pc.poi.stop_tracking()
                        logger.info(f"Pointing controller {self.pointing_controller_name} stopped tracking POI")
                        self.start_track_flag_old = self.start_track_flag
                else:
                    logger.warning(f"Pointing controller {self.pointing_controller_name} has no POI defined, cannot start tracking")

            data = self.pc.gimbal.telemetry_state.get()
            yaw = data.get("yaw", None)
            pitch = data.get("pitch", None)
            roll = data.get("roll", None)
            meta = {"yaw": round(yaw, 2) if yaw is not None else None, 
                    "pitch": round(pitch, 2) if pitch is not None else None, 
                    "roll": round(roll, 2) if roll is not None else None}
            self.status_board.beat("Gimbal", meta)

            if self.pc.poi is not None:
                data = self.pc.poi.get_data()
                distance = data.get("current_distance", None)
                tracking = data.get("is_tracking", None)
                meta = {"tracking": tracking, 
                        "distance": round(distance, 2) if distance is not None else None}
                self.status_board.beat("POI", meta)

        logger.info(f"Stopping Pointing Controller {self.pointing_controller_name}")
        try:
            if self.pc is not None:
                if self.pc.poi is not None:
                    self.pc.poi.stop_tracking()
                self.pc.stop_telemetry()
                self.pc.disconnect()
        except Exception as e:
            logger.error(f"Error stopping pointing controller: {e}")
            pass

    def start_tracking(self):
        self.start_track_flag = True
    
    def stop_tracking(self):
        self.start_track_flag = False


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
