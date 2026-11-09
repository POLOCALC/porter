import logging
import subprocess
import os
import signal
import time

from porter.process_utils import start_process, stop_process

logger = logging.getLogger(__name__)

# get the absolute path to this file's directory
current_dir = os.path.dirname(os.path.abspath(__file__))

class IMX5SensorModule:

    def __init__(self, name="IMX5 Sensors", device="/dev/ttyUSB0", baudrate=115200, model="Various", sensor_core=None):
        self.name = name
        self.model = model
        self.core = sensor_core
        self.device = device
        self.imu_rate = None
        self.ins_rate = None
        self.baudrate = baudrate
        self.process = None
        logger.info(f"Connected to IMX5 sensors {self.name}")

    def read_continous_binary(self, shutdown_flag, datafile_name, status_board):
        # start the IMX5SensorModule process through the command line
        cmd = f"IMX5SensorModule --imu-rate {self.imu_rate} --ins-rate {self.ins_rate} --baud-rate {self.baudrate} --outputdir {datafile_name} --device {self.device}"
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
                    imu_file = os.path.splitext(datafile_name)[0] + "_imu.csv"
                    if time.time() - os.path.getmtime(imu_file) > 5.0:
                        logger.error(f"{self.name}: no new data written for >5s, sensor may be disconnected")
                        break

                except OSError:
                    logger.error(f"{self.name}: no output file after startup grace period, sensor not writing")
                    break

            status_board.beat(self.name)

        self.close()

    def configure(self, config):
        self.imu_rate = config.get("imu_data_rate", 1000)
        self.ins_rate = config.get("ins_data_rate", 142)

        logger.info(f"Configured {self.name}")
        logger.info(f"Current IMX5 Sensors Data Rate: {self.imu_rate} Hz")
        logger.info(f"Current IMX5 INS Data Rate: {self.ins_rate} Hz")

    def close(self):
        # stop the process: SIGTERM, wait up to 3 s, then SIGKILL
        stop_process(self.process, self.name)
        self.process = None
        logger.info(f"Closed sensor {self.name}")