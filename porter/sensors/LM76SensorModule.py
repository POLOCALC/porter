import logging
import subprocess
import os
import signal
import time

logger = logging.getLogger(__name__)

# get the absolute path to this file's directory
current_dir = os.path.dirname(os.path.abspath(__file__))
# get the absolute path to the binary in the bin folder relative to this file's directory
binary_path = os.path.join(current_dir, "..", "..", "bin", "LM76SensorModule")

# Allowed values for the configuration register settings (passed to the binary)
INT_MODES = ("comparator", "event")
POLARITIES = ("active-low", "active-high")


class LM76SensorModule:

    def __init__(
        self,
        name="LM76 Temperature Sensor",
        bus="/dev/i2c-1",
        address="0x4B",
        model="LM76",
        sensor_core=None,
    ):

        self.name = name
        self.model = model
        self.core = sensor_core
        self.bus = bus
        self.address = address
        self.interval = 10  # Default to 10 seconds
        self.tcrit = None
        self.thyst = None
        self.tlow = None
        self.thigh = None
        # Configuration register settings (None leaves the register bit untouched)
        self.int_mode = None
        self.tcrit_polarity = None
        self.int_polarity = None
        self.fault_queue = None

        self.process = None

        logger.info(f"Connected to LM76 sensor {self.name}")

    def read_continous_binary(self, shutdown_flag, datafile_name, status_board):
        # Start the LM76SensorModule process through the command line.
        cmd = f"{binary_path} --bus {self.bus} --address {hex(self.address)} --interval {self.interval} --outputdir {datafile_name}"
        
        # Add threshold configurations if set
        if self.tcrit is not None:
            cmd += f" --tcrit {self.tcrit}"
        if self.thyst is not None:
            cmd += f" --thyst {self.thyst}"
        if self.tlow is not None:
            cmd += f" --tlow {self.tlow}"
        if self.thigh is not None:
            cmd += f" --thigh {self.thigh}"

        # Add configuration register settings if set
        if self.int_mode is not None:
            cmd += f" --int-mode {self.int_mode}"
        if self.tcrit_polarity is not None:
            cmd += f" --tcrit-polarity {self.tcrit_polarity}"
        if self.int_polarity is not None:
            cmd += f" --int-polarity {self.int_polarity}"
        if self.fault_queue is not None:
            cmd += f" --fault-queue {'enabled' if self.fault_queue else 'disabled'}"
        
        if self.core is not None:
            cmd += f" --core {int(self.core)}"

        logger.info(f"Running command: {cmd}")
        # print(f"Running command: {cmd}")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            preexec_fn=os.setsid,
        )
        launch_time = time.monotonic()
        # The binary writes <name>.csv next to the given .bin path (see main.rs)
        csv_file = os.path.splitext(datafile_name)[0] + ".csv"
        # Allow one missed sample plus scheduling jitter before declaring the sensor dead
        max_silence = self.interval * 2 + 5.0
        # Loop until told to close

        while not shutdown_flag.is_set():
            shutdown_flag.wait(1)
            # Check if the process exited on its own
            if self.process is not None and self.process.poll() is not None:
                logger.error(f"{self.name} process exited unexpectedly (exit code {self.process.returncode})")
                break
            # After startup grace period, verify data is still flowing to the CSV file
            if time.monotonic() - launch_time > 10.0:
                try:
                    if time.time() - os.path.getmtime(csv_file) > max_silence:
                        logger.error(f"{self.name}: no new data written for >{max_silence:.0f}s, sensor may be disconnected")
                        break
                except OSError:
                    pass
            status_board.beat(self.name)
            
        self.close()

    def configure(self, config):
        self.interval = config.get("interval", 1)
        self.tcrit = config.get("tcrit", None)
        self.thyst = config.get("thyst", None)
        self.tlow = config.get("tlow", None)
        self.thigh = config.get("thigh", None)
        self.int_mode = self._parse_choice(config, "int_mode", INT_MODES)
        self.tcrit_polarity = self._parse_choice(config, "tcrit_polarity", POLARITIES)
        self.int_polarity = self._parse_choice(config, "int_polarity", POLARITIES)
        self.fault_queue = config.get("fault_queue", None)
        if self.fault_queue is not None and not isinstance(self.fault_queue, bool):
            raise ValueError(
                f"{self.name}: fault_queue must be true or false, got {self.fault_queue!r}"
            )

        logger.info(f"Configured {self.name}")
        logger.info(f"Current LM76 Reading Interval: {self.interval} seconds")
        if self.tcrit is not None:
            logger.info(f"T_CRIT threshold: {self.tcrit}°C")
        if self.thyst is not None:
            logger.info(f"THYST threshold: {self.thyst}°C")
        if self.tlow is not None:
            logger.info(f"TLOW threshold: {self.tlow}°C")
        if self.thigh is not None:
            logger.info(f"THIGH threshold: {self.thigh}°C")
        if self.int_mode is not None:
            logger.info(f"INT mode: {self.int_mode}")
        if self.tcrit_polarity is not None:
            logger.info(f"T_CRIT_A polarity: {self.tcrit_polarity}")
        if self.int_polarity is not None:
            logger.info(f"INT polarity: {self.int_polarity}")
        if self.fault_queue is not None:
            logger.info(f"Fault queue: {'enabled' if self.fault_queue else 'disabled'}")

    def _parse_choice(self, config, key, allowed):
        # Validate here, since the binary's stderr is not read and a bad value
        # would make it exit silently
        value = config.get(key, None)
        if value is None:
            return None
        value = str(value).strip().lower().replace("_", "-")
        if value not in allowed:
            raise ValueError(
                f"{self.name}: invalid {key} {config[key]!r}, expected one of {allowed}"
            )
        return value

    def close(self):

        # Kill the process
        if self.process is not None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass  # process already gone
            self.process = None

        logger.info(f"Closed sensor {self.name}")

