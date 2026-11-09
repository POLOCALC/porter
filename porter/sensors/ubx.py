import logging
import time
from datetime import datetime
import io
import copy
import serial

import pyubx2 as ubx
from pynmeagps import NMEA_HDR


logger = logging.getLogger(__name__)

HEADER = b"\xb5\x62"


def find_baudrate(port, logger, baudrates=[38400, 57600, 115200, 230400], timeout=1.0):

    brate_found = False
    
    correct = 38400

    for brate in baudrates:

        if brate_found:
            pass
        else:
            conn = serial.Serial(port, brate, timeout=timeout)
            logger.info(f"Attempting Baudrate {brate}")
            conn.read(conn.inWaiting())
            poll = ubx.UBXMessage("MON", "MON-VER", ubx.POLL)
            conn.reset_input_buffer()
            conn.write(poll.serialize())

            data = conn.read(1000)
            i = 0
            while i < (len(data) - 1):

                header = copy.copy(data[i : i + 2])

                if header == b"\xb5\x62" or header in NMEA_HDR:
                    logger.info(f"Found Baudrate @ {brate}")
                    reader = ubx.UBXReader(io.BytesIO(data))
                    res, parsed = reader.read()
                    try:
                        logger.info(f"{type(parsed.identity)} ---- {parsed.identity}")
                    except:
                        logger.info(f"Message not recorded")
                    brate_found = True
                    correct = copy.copy(brate)
                    i = 10000
                    break

                i += 1

            conn.read(conn.inWaiting())
            conn.close()
            time.sleep(0.2)

    return correct


class UBX:

    def __init__(self, port, baudrate, name):

        self.name = name
        self.__new_baudrate = False
        # persistent metadata: only updated when a NAV-STATUS message arrives
        self.metadata = {}

        if baudrate != 38400:
            # Set parameters for updating baudrate of the GPS.
            self.__new_baudrate = True
            self.__port = port
            self.__brate = int(baudrate)

        else:
            # Open a serial connection with the ZED-F9P at the default baud rate if not configured otherwise.
            self.conn = serial.Serial(port, 38400, timeout=1)
            if self.conn.is_open:
                logger.info(f"Connected to ublox sensor {self.name} @ {38400}")
                self.reader = ubx.UBXReader(self.conn, protfilter=2)

        self.timing_results = ""
        self.identities = ""

    def configure(self, config):
        layers = 1
        transaction = 0
        keys = []

        # parsing yaml config file keys and converting them to configuration keys that the ZED-F9P can interpret.
        for i in config.keys():
            if i.lower() == "rate":
                # add rate configuration
                rate = int(1 / config["RATE"]["value"] * 1000)
                keys.append(("CFG_RATE_MEAS", rate))

            elif i.lower() == "ubx_msg":
                # add output port for UBX messages.
                output_port = config["UBX_MSG"]["output_port"]

                if output_port[0].lower() == "uart":
                    port_string = "UART" + str(int(output_port[1]))
                else:
                    port_string = output_port

                string = "CFG_MSGOUT_UBX_"
                # enable options for logging different UBX messages.
                for j in config["UBX_MSG"].keys():
                    if j.lower() == "output_port":
                        pass
                    else:
                        for k in config["UBX_MSG"][j]:
                            msg = string + j + "_" + k + "_" + port_string
                            keys.append((msg, 1))

            elif i.lower() == "nmea_msg":

                # add output port for NMEA messages.
                output_port = config["NMEA_MSG"]["output_port"]

                if output_port[0].lower() == "uart":
                    port_string = "UART" + str(int(output_port[1]))
                else:
                    port_string = output_port

                string = "CFG_MSGOUT_NMEA_ID"

                # enable options for logging different NMEA messages.
                for j in config["NMEA_MSG"].keys():
                    if j.lower() == "output_port":
                        pass
                    else:
                        for k in config["NMEA_MSG"][j]:
                            msg = string + "_" + k + "_" + port_string

                            keys.append((msg, 1))

            # configuring output port configurations for the ZED-F9P
            elif i[:4].lower() == "nmea" or i[:3].lower() == "ubx":

                if i[:4].lower() == "nmea":
                    p = i[:4]
                else:
                    p = i[:3]

                if config[i]["output"]["set"]:
                    output = 1
                else:
                    output = 0

                string = "CFG_" + config[i]["output"]["port"] + "OUTPROT_" + p
                keys.append((string, output))

            else:
                if isinstance(config[i], list):
                    keys.append((config[i][0], config[i][1]))

        # setting up and serialize the configuration parameters for the ZED-F9P
        cfgs = ubx.UBXMessage.config_set(layers, transaction, keys)
        serial_cfgs = cfgs.serialize()

        if self.__new_baudrate:
            # self.conn.reset_input_buffer()

            # set the ZED-F9P baudrate to the one specified in the config file.
            msg_baud = ubx.UBXMessage.config_set(
                1, 0, [("CFG_UART1_BAUDRATE", self.__brate)]
            )

            time.sleep(0.5)
            baudrate_temp = 0
            count = 0
            loops = 10

            while count < loops:
                baudrate_temp = find_baudrate(self.__port, logger)

                if baudrate_temp == self.__brate:
                    logger.info(f"Correct baudrate found @ {baudrate_temp}")
                    logger.info("Baudrate has been changed correctly")
                    count = 15
                    break

                self.conn = serial.Serial(self.__port, baudrate_temp, timeout=1)
                self.conn.write(msg_baud.serialize())

                logger.info(f"Baudrate Message written with current Baudrate @ {baudrate_temp}")
                time.sleep(0.2)

                t0 = time.perf_counter()
                while time.perf_counter() - t0 <= 1.0:
                    self.conn.reset_input_buffer()

                self.conn.close()
                time.sleep(0.2)
                count += 1

            if count >= loops:
                self.__brate = baudrate_temp
                logger.info(f"Baudrate Set @ {self.__brate}")

            time.sleep(0.5)

            # Reopen a serial connection at the new baudrate.
            self.conn = serial.Serial(self.__port, self.__brate, timeout=1)

            if self.conn.is_open:
                logger.info(f"Connected to ublox sensor {self.name} @ {self.__brate}")
                self.reader = ubx.UBXReader(self.conn, protfilter=2)
        
        for i in range(2):
            self.conn.write(serial_cfgs)
            t0 = time.perf_counter()
            self.conn.read(self.conn.inWaiting())

            while time.perf_counter() - t0 <= 1.0:
                logger.info(f"Bytes  === {self.conn.inWaiting()}")
                parsed = self.read(parsing=True)
                if parsed is None:
                    continue
                if parsed.identity == "ACK-ACK":
                    logger.info(f"Output Configuration {parsed.identity}")
                    break
                else:
                    logger.info(f"Output Configuration {parsed.identity}")

            logger.info(f"Configuration {keys}")

    def read_continous_binary(self, shutdown_flag, datafile_name, status_board):
        consecutive_timeouts = 0
        last_flush = time.monotonic()

        with open(datafile_name, "ab") as datafile:   # also closes the file on any exit
            while not shutdown_flag.is_set():
                try:
                    msg, parsed = self.read(parsing=None)
                except Exception as e:
                    logger.error(f"{self.name} read error, sensor may have disconnected: {e}")
                    break

                if msg is None:                        # serial timeout, no bytes
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 10:     # about 10 seconds of silence
                        logger.error(f"{self.name}: no data for {consecutive_timeouts}s")
                        break
                    continue                           # don't beat: let the status show it
                consecutive_timeouts = 0

                datafile.write(msg)
                if time.monotonic() - last_flush > 1.0:
                    datafile.flush()                   # at most about 1 second lost on a hard stop
                    last_flush = time.monotonic()
            
                # update fix status only when a NAV-STATUS message arrives;
                # all other message types leave the last known status intact
                if parsed is not None and parsed.identity == "NAV-STATUS":
                    self.metadata.update({"status": parsed.gpsFix, "status_ok": parsed.gpsFixOk})

                # signal strength from the raw measurements: tracked satellites and top-4 C/N0
                if parsed is not None and parsed.identity == "RXM-RAWX":
                    best = {}                                   # (gnssId, svId) -> strongest C/N0 of its signals
                    for i in range(1, parsed.numMeas + 1):
                        cno = getattr(parsed, f"cno_{i:02d}", 0)
                        key = (getattr(parsed, f"gnssId_{i:02d}", None), getattr(parsed, f"svId_{i:02d}", None))
                        if cno > 0 and cno > best.get(key, 0):
                            best[key] = cno
                    top = sorted(best.values(), reverse=True)[:4]
                    self.metadata.update({
                        "sats": len(best),
                        "cno":  round(sum(top) / len(top), 1) if top else 0,
                    })


                # add lat lon and alt to metadata if available
                if parsed is not None and parsed.identity == "NAV-POSLLH":
                    lat, lon, alt = parsed.lat, parsed.lon, parsed.hMSL / 1000.0     # m above mean sea level
                    self.metadata["position"] = (lat, lon, alt, time.monotonic())
                    self.metadata.update({"latitude": lat, "longitude": lon, "altitude": alt})

                # update the status board
                status_board.beat(self.name, {k: v for k, v in self.metadata.items() if k != "position"})

            self.close()

    def get_gnss_source(self):
        return self.metadata

    def read(self, parsing=False):
        # Read from the UBX reader
        raw, parsed = self.reader.read()
        if parsing is None:
            return raw, parsed
        elif parsing:
            return parsed
        else:
            return raw

    def close(self):
        # Turn off the serial connection
        self.conn.close()
        logger.info(f"Closed ublox sensor {self.name}")
