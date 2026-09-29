import logging
import subprocess
import os
import signal
import time

from porter.process_utils import start_process, stop_process

logger = logging.getLogger(__name__)

# get the absolute path to this file's directory
current_dir = os.path.dirname(os.path.abspath(__file__))

class Inertial:
    def __init__(self, name="Inertial Sensors", bus=4, model="Various", sensor_core=None):
        self.name = name 
        self.model = model 
        self.core = sensor_core
        self.bus = int(bus)
        self.rate = None
        self.process = None
        logger.info(f"Connected to inertial sensors {self.name}")

    def read_continous_binary(self, shutdown_flag, datafile_name, status_board):
        # start the inertial process through the command line
        cmd = f"inertial --rate {self.rate} --outputdir {datafile_name} --i2c-bus {self.bus} --no-imu"
        logger.warning("Inertial sensors: IMU is disabled (hardcoded in porter/sensors/inertial.py)")
        if self.core is not None:
            cmd += f" --core {int(self.core)}"
            
        # send the binary output to a log file next to the data file instead of an
        # unread pipe, so it can't block on a full pipe and its errors are kept
        log_path = os.path.splitext(datafile_name)[0] + "_stdout.log"
        with open(log_path, "ab") as log:
            self.process = start_process(cmd, stdout=log, stderr=subprocess.STDOUT)
        launch_time = time.monotonic()

        # loop until told to close
        while not shutdown_flag.is_set():
            shutdown_flag.wait(1)
            # check if the process exited on its own
            if self.process is not None and self.process.poll() is not None:
                logger.error(f"{self.name} process exited unexpectedly (exit code {self.process.returncode})")
                break
            # after startup grace period, verify data is still flowing to the output directory
            if time.monotonic() - launch_time > 10.0:
                try:
                    # get list of files in the output directory
                    files = os.listdir(datafile_name)
                    # check if any file has been modified in the last 5 seconds
                    if not any(time.time() - os.path.getmtime(os.path.join(datafile_name, f)) < 5.0 for f in files):
                        logger.error(f"{self.name}: no new data written for >5s, sensor may be disconnected")
                        break
                except OSError:
                    logger.error(f"{self.name}: no output file after startup grace period, sensor not writing")
                    break

            status_board.beat(self.name)

        self.close()

    def configure(self, config):
        self.rate = config.get("data_rate", 200)

        logger.info(f"Configured {self.name}")
        logger.info(f"Current Inertial Sensors Data Rate: {self.rate} Hz")

    def close(self):
        # stop the process: SIGTERM, wait up to 3 s, then SIGKILL
        stop_process(self.process, self.name)
        self.process = None
        logger.info(f"Closed sensor {self.name}")