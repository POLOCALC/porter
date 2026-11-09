import logging
import random
import struct
import time

logger = logging.getLogger(__name__)


class FakeConnection:

    def __init__(self, name):
        self.name = name
        self._time_sample = 0.001
        logger.info(f"Created fake sensor for {name}")

    def read_continous_binary(self, fs, flag, sampling=1.0 / 100.0, delta=1e-8):
        while not flag.is_set():
            msg = struct.pack("<d", time.time())
            tstart = time.perf_counter()
            msg += bytes(int(random.random()))
            while time.perf_counter() - tstart < (sampling - delta):
                pass

    def read(self, chunk_size=100):
        count = 0
        while count < chunk_size:
            val = bytes(int(random.random()))
            count += 1
        return val

    def close(self):
        logger.info(f"Closed fake sensor {self.name}")
