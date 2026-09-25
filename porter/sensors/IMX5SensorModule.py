import logging
import subprocess
import os
import signal
import time

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

        logger.info(f"Running command: {cmd}")
        # print(f"Running command: {cmd}")
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, preexec_fn=os.setsid)
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
                    # get list of files in the output directory that start with "IMX5"
                    files = [f for f in os.listdir(os.path.dirname(datafile_name)) if f.startswith("IMX5")]
                    # check if any file has been modified in the last 5 seconds
                    if not any(time.time() - os.path.getmtime(os.path.join(os.path.dirname(datafile_name), f)) < 5.0 for f in files):
                        logger.error(f"{self.name}: no new data written for >5s, sensor may be disconnected")
                        break
                except OSError:
                    logger.error(f"Failed to read sensor output directory: {datafile_name}")
                    pass
            status_board.beat(self.name)

        self.close()

    def configure(self, config):
        self.imu_rate = config.get("imu_data_rate", 1000)
        self.ins_rate = config.get("ins_data_rate", 142)

        logger.info(f"Configured {self.name}")
        logger.info(f"Current IMX5 Sensors Data Rate: {self.imu_rate} Hz")
        logger.info(f"Current IMX5 INS Data Rate: {self.ins_rate} Hz")

    def close(self):

        # Kill the process
        if self.process is not None:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process = None

        logger.info(f"Closed sensor {self.name}")
