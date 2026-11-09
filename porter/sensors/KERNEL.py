import copy
import logging
import pickle
import time

import serial
import struct

import porter.sensors.KERNEL_utils as utils
import porter.sensors.sensors_db.KERNEL as Kdb

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
            length_byte = len(msg).to_bytes(1, byteorder="little")

            payload = length_byte + msg

            msg = (
                utils.HEADER
                + b"\x00"
                + b"\x00"
                + struct.pack("<H", len(payload))
                + payload
            )

            chk = utils._checksum(msg)

            return msg + chk, chk

    def convert_ack_UDD(self, data):

        def check_byte(byte):
            if isinstance(byte, int):
                return format(byte, "08b")  # Ensure 8-bit representation
            elif isinstance(byte, str):
                return format(int(byte, base=16), "08b")  # Convert hex to binary

        vals = {}

        length = [1, 2, 1, 1]
        name = ["Errors", "Correctness", "Max Data Rate", "Reserved"]

        error_table = [
            "Structure",
            "Data",
            "Data Type",
            "Accepted Rate",
            "Reserved",
            "Reserved",
            "Reserved",
            "Reserved",
        ]

        start = 0

        res = {}
        for i in range(len(name)):

            bits = check_byte(data[start : start + length[i]])
            result = []
            if name[i] == "Errors":
                for j, bit in enumerate(reversed(bits)):
                    if error_table[j] == "Reserved":
                        pass
                    else:
                        result.append(
                            f"KO_{error_table[j]}"
                            if bit == "1"
                            else f"OK_{error_table[j]}"
                        )

            else:
                result.append(bits)

            res[name] = result

            start += length[i]

        return res

    def configure(self, config):

        ### Read Current Configuration to log Important data ###

        msg = utils.HEADER + b"\x00" + b"\x00" + b"\x07" + b"\x00" + b"\x41"

        chk = utils._checksum(msg)
        self.conn.write(msg + chk)

        count = 0

        self.__alignment_time = 2

        while count < 20:
            temp = self.conn.read_until(expected=utils.HEADER)[:-2]

            if temp[1:2] == b"\x41":
                (data_rate,) = struct.unpack("<H", temp[4:6])
                (self.__alignment_time,) = struct.unpack("<H", temp[6:8])

                logger.info(f"Current Data Rate {data_rate} Hz")
                logger.info(f"Alignment Time {self.__alignment_time} s")

                break
            count += 1

        mode = config["mode"]

        self._INC_mode = mode

        self.conn.reset_input_buffer()

        if mode == "USER_DEFINED_DATA":
            msg_1, _ = self.payload_cmds("USER_DEFINED_DATA_CONFIG")
            msg_2, chk2 = self.payload_UDD(config["UDD_data"])

            self.conn.write(msg_1)
            self.conn.write(msg_2)
            
            time.sleep(0.2)
            ack = self.conn.read(15)
            val = copy.copy(ack[6:8])

            if val == chk2:
                logger.info("UDD Right")
                add = copy.copy(ack[8:13])
                print("ADD", add)
                res = self.convert_ack_UDD(add)

                for r in res.keys():
                    logger.info(f"Name: {r} with the following payload {res[r]}")

        msg, chk = self.payload_cmds(mode)

        self.conn.write(msg)
        time.sleep(0.5)

        ack = self.conn.read(10)

        val = copy.copy(ack[6:8])

        if val == chk:
            logger.info("Sent message to start collecting Inclinometer data")
            logger.info(f"Mode Used: {mode}")
            logger.info(f"MSG: {msg}")
            logger.info(f"ACK: {ack}")
        else:
            logger.info("Cannot connect to inclinometer")

    def read_continous_binary(self, flag, fs, chunk_size=1000):

        try:
            datafile = open(fs, "r+b")
        except FileNotFoundError:
            datafile = open(fs, "x+b")

        time.sleep(self.__alignment_time)

        self.conn.reset_input_buffer()

        logger.info(f"Start collecting data from {self.name} @ {time.time()}")

        while not flag.is_set():
            datafile.write(self.conn.read(chunk_size))
        self.close()

    def read(self, chunk_size=None):

        if self.__first_msg:
            msg, length = self._find_msg()

        else:
            if self.expected_length is not None:
                msg = self.conn.read(self.expected_length)
            else:
                msg = self.conn.read(chunk_size)

        return msg

    def close(self):

        msg = utils.HEADER + b"\x00" + b"\x00" + b"\x07" + b"\x00" + b"\xfe"

        chk = utils._checksum(msg)
        self.conn.write(msg + chk)

        self.conn.close()

        logger.info(f"Closed sensor {self.name}")

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
            try:
                msg_class = utils.KernelMsg()
                msg = msg_class.decode_single(msg, return_dict=return_dict)
            except ValueError:
                pass

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
