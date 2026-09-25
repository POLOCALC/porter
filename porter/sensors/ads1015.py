import logging
import subprocess
import os
import signal
import time

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
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True, preexec_fn=os.setsid) 
        launch_time = time.monotonic()
        # Loop until told to close

        while not shutdown_flag.is_set():
            shutdown_flag.wait(1)
            # Check if the process exited on its own
            if self.process is not None and self.process.poll() is not None:
                logger.error(f"{self.name} process exited unexpectedly (exit code {self.process.returncode})")
                break
            # After startup grace period, verify data is still flowing to the output file
            if time.monotonic() - launch_time > 10.0:
                try:
                    if time.time() - os.path.getmtime(datafile_name) > 5.0:
                        logger.error(f"{self.name}: no new data written for >5s, sensor may be disconnected")
                        break
                except OSError:
                    pass
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
        # kill the process
        if self.process is not None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass  # process already gone
            self.process = None

        logger.info(f"Closed sensor {self.name}")
