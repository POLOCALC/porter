import copy
import logging
import pickle
import time

import serial

import sys

sys.path.insert(0, "/home/polocalc/Documents/porter/porter/sensors")
sys.path.insert(0, "/home/polocalc/Documents/porter/porter/sensors/sensors_db")

import KERNEL_utils_test as utils
import KERNEL_test as Kdb

logger = logging.getLogger(__name__)


class KernelInertial:

    def __init__(self, port, baudrate, **kwargs):

        self.port = port
        self.baudrate = baudrate

        self.__first_msg = True

        self.conn = serial.Serial(port, baudrate=baudrate, timeout=1)

        self.name = kwargs.get("name", "Generic Kernel")

        if self.conn.is_open:
            logger.info(f"Connected to KERNEL sensor {self.name}")

    def _check_rate(self, mode):

        length = Kdb.MODES[mode]["length"] + 8
        bits_per_sample = 11

        rate_max = self.baudrate / bits_per_sample / length

        rate_max = int(5 * round(rate_max / 5))

        return rate_max

    def payload_cmds(self, mode):

        msg = (
            utils.HEADER
            + b"\x00"
            + b"\x00"
            + b"\x07"
            + b"\x00"
            + Kdb.MODES[mode]["Address"]
        )

        chk = utils._checksum(msg)

        return msg + chk, chk

    def payload_UDD(self, data):

        if isinstance(data, bytes):
            return data

        elif isinstance(data, list):
            msg = b""

            for i in data:
                if isinstance(i, str):
                    msg += Kdb.User_Defined_Data[i]["Address"]

            return msg

    def configure(self, config):

        mode = config["mode"]

        self._INC_mode = mode

        if mode == "USER_DEFINED_DATA":
            msg_1, chk = self.payload_cmds(mode)
            msg_2 = self.payload_UDD(config["UDD_data"])

            self.conn.write(msg_1)
            self.conn.write(msg_2)
        else:
            msg, chk = self.payload_cmds(mode)

            self.conn.write(msg)

        ack = self.conn.read(10)

        val = copy.copy(ack[6:8])

        if val == chk:
            logger.info("Sent message to start collecting Inclinometer data")
            logger.info(f"Mode Used: {mode}")
        else:
            logger.info("Cannot connect to inclinometer")

    def read_continous_binary(self, fs, flag, sensor_lock):

        while not flag.is_set():
            
            fs.write(self.read(sensor_lock))

        self.close()

    def read(self, sensor_lock, chunk_size=None):

        if self.__first_msg:
            msg, length = self._find_msg()

        else:
            sensor_lock.acquire()
            if self.expected_length is not None:
                msg = self.conn.read(self.expected_length)
            else:
                msg = self.conn.read(chunk_size)
            sensor_lock.release()

        return msg

    def close(self):

        msg = utils.HEADER + b"\x00" + b"\x00" + b"\x07" + b"\x00" + b"\xFE"

        chk = utils._checksum(msg)
        self.conn.write(msg + chk)

        self.conn.close()

        logging.info(f"Closed sensor {self.name}")

    def _find_msg(self, waiting=2):
        """Find the first message available with output data"""

        if self.__first_msg:
            time.sleep(waiting)

        temp = self.conn.read_until(expected=utils.HEADER)[:-2]
        pre = self.conn.read(4)

        length = int.from_bytes(pre[2:3], byteorder="little", signed=False)

        if self.__first_msg:
            self.expected_length = copy.copy(length + 2)
            self.__first_msg = False
            logger.info(f"Read First Message from {self.name}")

        payload = self.conn.read(length - 4)

        if len(payload) > len(temp) - 4:
            msg = utils.HEADER + pre + payload
        else:
            msg = temp

        return msg, length

    def read_single(self, decode=False, return_dict=False):
        """Read the first single message available from a Kernel Device"""

        msg, _ = self._find_msg()

        if decode:
            msg_class = utils.KernelMsg()
            msg = msg_class.decode_single(msg, return_dict=return_dict)

        return msg

    def _save_binary(self, chunks_size=50, max_bytes=None, filename="INC_Data"):

        count = 0

        pck = open(filename + "_" + self._INC_mode, "ab")

        while True:

            pickle.dump(self.conn.read(chunks_size), pck)

            if max_bytes is not None:
                if count > int(max_bytes / chunks_size):
                    break
            count += 1

        logger.info("Stop Collecting Data")

    def stream_data(self, max_counter=None):

        msg_class = utils.KernelMsg()

        first_msg, length = self._find_msg()

        msg = msg_class.decode_single(first_msg, return_dict=True)

        counter = 0

        while True:
            temp = self.conn.read(length)
            if counter > max_counter:
                break
            counter += 1
