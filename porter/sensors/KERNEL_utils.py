import copy
import pickle
import struct

import porter.sensors.sensors_db.KERNEL as Kdb

HEADER = b"\xaa\x55"


def _checksum(msg):
    """Compute the checksum of a message

    The checksum is the arithmetical sum of all the bytes in the message
    excluding the header. The resulting sum is an unsigned short integer
    whose Least Significant Byte is first

    Args:
        msg (bytes): message used to compute the checksum

    Return:
        checksum (bytes)

    """

    if msg.startswith(HEADER):
        msg = msg[2:]

    return sum(msg).to_bytes(2, byteorder="little", signed=False)


class KernelMsg:

    def __init__(self):

        self.msg_address = []

        for i in Kdb.MODES.keys():
            self.msg_address.append(Kdb.MODES[i]["Address"])

    def decode_single(self, msg, UDD=False, UDD_keys=None):
        """Decode a single message sent by the inclinometer

        The structure of the message is presented in the KERNEL IMU ICD v1.27

        Args:
            msg (bytes): message to be decoded
        Return:
            vals ()
        """

        if msg[:2] == HEADER:
            type_idx = 3
        else:
            type_idx = 1

        msg_type = msg[type_idx].to_bytes(1, byteorder="little")
        modes = list(Kdb.MODES.keys())

        vals = {}

        idx = self.msg_address.index(msg_type)

        vals["Type"] = modes[idx]

        start = type_idx + 3

        print(vals["Type"], UDD_keys)

        if UDD:
            for key in UDD_keys:
                print(key, Kdb.User_Defined_Data[key]["Name"])

                fmt = "<" + "".join(Kdb.User_Defined_Data[key]["Struct"])
                val = msg[start : start + struct.calcsize(fmt)]

                print(key, fmt, struct.calcsize(fmt), len(msg), val)

                if key != "USW":
                    print("OK")
                    tmp = struct.unpack(fmt, val)
                    print("KEY", tmp)
                    # vals[Kdb.User_Defined_Data[key]["Name"]] = (
                    #    tmp / Kdb.User_Defined_Data[key]["Scale"]
                    # )
                else:
                    tmp = Kdb.extract_USW(val)
                    vals[Kdb.User_Defined_Data[key]["Name"]] = tmp

                start += struct.calcsize(fmt)
        else:
            try:
                for i in range(len(Kdb.MODES[modes[idx]]["Type"])):
                    mm = Kdb.MODES[modes[idx]]["Parameters"][i]

                    fmt = "<" + "".join(Kdb.MODES[modes[idx]]["Type"][i])
                    val = msg[
                        start : start
                        + struct.calcsize(Kdb.MODES[modes[idx]]["Type"][i])
                    ]

                    if Kdb.MODES[modes[idx]]["Parameters"][i] != "USW":
                        (tmp,) = struct.unpack(fmt, val)
                        vals[Kdb.MODES[modes[idx]]["Parameters"][i]] = (
                            tmp / Kdb.MODES[modes[idx]]["Scale"][i]
                        )
                    else:
                        tmp = Kdb.extract_USW(val)
                        vals[Kdb.MODES[modes[idx]]["Parameters"][i]] = tmp

                    start += struct.calcsize(Kdb.MODES[modes[idx]]["Type"][i])
            except KeyError:
                pass

        return vals

    def decode_multi(self, filename, UDD=False, UDD_keys=None):
        """Decode multiple messages saved in a binary file"""

        count = 0

        with open(filename, "rb") as fd:
            if filename[-3:].lower() == ".pck":
                data = pickle.load(fd)
            else:
                data = fd.read()

        parts = data.split(HEADER)

        # Reattach the header to each split part (except the first, which was before the first header)
        messages = [HEADER + part for part in parts[1:]]

        data = data[data.find(HEADER) :]

        decoded = {}
        count = 0

        for msg in messages:
            try:
                tmp = self.decode_single(msg, UDD=UDD, UDD_keys=UDD_keys)

                for j in tmp.keys():
                    if count == 0:
                        decoded[j] = []

                    decoded[j].append(tmp[j])

            except struct.error:
                pass
            except ValueError:
                pass
            except IndexError:
                pass

            count += 1

        return decoded
