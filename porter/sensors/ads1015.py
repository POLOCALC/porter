import logging
import os
import subprocess
import signal
import time

from porter.process_utils import start_process, stop_process

ADS1015_VALUE_GAIN = {
    1: 4.096,
    2: 2.048,
    4: 1.024,
    8: 0.512,
    16: 0.256,
}

logger = logging.getLogger(__name__)

# get the absolute path to this file's directory
current_dir = os.path.dirname(os.path.abspath(__file__))

class ADS1015:

    def __init__(self, name="Generic ADC", bus=6, model="ADS1015", sensor_core=None):
        self.name = name 
        self.model = model 
        self.core = sensor_core
        self.bus = int(bus)
        self.gain = None
        self.rate = None
        self.gain_value = None
        self.process = None
        logger.info(f"Connected to ADC {self.name}")

    def read_continous_binary(self, shutdown_flag, datafile_name, status_board):
        # start the ads1015 process through the command line
        cmd = f"ads1015 --gain {self.gain} --rate {self.rate} --output {datafile_name} --i2c-bus {self.bus}"
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
            # after startup grace period, verify data is still flowing to the output file
            if time.monotonic() - launch_time > 10.0:
                try:
                    if time.time() - os.path.getmtime(datafile_name) > 5.0:
                        logger.error(f"{self.name}: no new data written for >5s, sensor may be disconnected")
                        break
                except OSError:
                    logger.error(f"{self.name}: no output file after startup grace period, sensor not writing")
                    break

            status_board.beat(self.name)

        self.close()

    def configure(self, config):
        self.gain = config.get("gain", 8)
        self.rate = config.get("data_rate", 1600)
        self.gain_value = ADS1015_VALUE_GAIN[self.gain] 

        logger.info(f"Configured {self.name}")
        logger.info(f"Current ADC Data Rate: {self.rate} Hz")
        logger.info(f"Current Gain Setting {self.gain}: {self.gain_value}")

    def close(self):
        # stop the process: SIGTERM, wait up to 3 s, then SIGKILL
        stop_process(self.process, self.name)
        self.process = None
        logger.info(f"Closed sensor {self.name}")
