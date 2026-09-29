import logging
import time

import serial

logger = logging.getLogger(__name__)


class Valon:
    """
    Interface to a valon synthetizer using a serial interface
    """

    def __init__(self, port, baud):
        """
        Parameters:
        - port: serial port for connection
        - baud: baudrate for the serial connection
        """
        self.conn = None
        try:
            self.conn = serial.Serial(port, baud, timeout=1)
            self.conn.reset_input_buffer()
            logger.info(f"Connected to Valon on {port} at {baud} baud")
        except serial.SerialException as e:
            raise RuntimeError(f"Valon not available on {port}: {e}")


    def send_receive(self, msg, receive=True):
        """
        Send and receive a message through a serial connection
        Parameters:
        - msg: Message to be sent (string)
        - receive: if True, the reply from the serial connection will be returned
        """
        self.conn.write(str.encode(msg))

        if receive:
            time.sleep(0.1)
            resp_size = self.conn.inWaiting()
            resp = self.conn.read(resp_size)
            logger.info(resp)
            return resp.decode(errors="replace").splitlines()

        else:
            if self.conn.inWaiting() > 0:
                self.conn.reset_input_buffer()

    def get_id(self):
        """
        Get id
        """
        msg = "id\r"
        id_raw = self.send_receive(msg)
        if id_raw is None or len(id_raw) < 2:
            logger.error("Failed to get ID from Valon")
            return None
        return str(id_raw)

    def get_stat(self):
        """
        Get status (in order to see if there are firmware updates)
        """
        msg = "stat\r"
        stat_raw = self.send_receive(msg)
        stat_raw = stat_raw[1].split(",")
        return str(stat_raw[3])

    def set_freq(self, f):
        """
        Set the frequency output of the Valon.
        Parameters:
        - f: frequency in MHz
        """
        msg = "f " + str(f) + "\r"
        self.send_receive(msg, receive=True)

    def get_freq(self):
        """
        Get current frequency output of the Valon in MHz
        """
        msg = "f?\r"
        freq_raw = self.send_receive(msg)
        # freq_raw = freq_raw[1].split('//')
        # return float(freq_raw[1][4:-4])
        return freq_raw

    def set_pwr(self, pwr):
        """
        Set the power output of the Valon.
        Parameters:
        - pwr: power in dBm
        """

        msg = "pwr " + str(pwr) + "\r"
        self.send_receive(msg, receive=True)

    def get_pwr(self):
        """
        Get current power output of the Valon in dBm
        """
        msg = "pwr?\r"
        pwr_raw = self.send_receive(msg)
        # pwr_raw = pwr_raw[1].split(';')
        # return float(pwr_raw[0][4:])
        return pwr_raw

    def set_amd(self, amd_db, amd_f):
        """
        Set the AM modulation output of the Valon in dB. AM frequency can be from 1 Hz
        to 2 KHz with 1 Hz resolution
        Parameters:
        - amd_db: AM modulation in dB. the range is from 0 to 20.0 dB)
        - amd_f: AM modulation in Hz. the range is from 0.5 Hz and 10kHz
        """
        msg_db = "amd " + str(amd_db) + "\r"
        self.send_receive(msg_db, receive=True)
        msg_f = "amf " + str(amd_f) + "\r"
        self.send_receive(msg_f, receive=True)

    def get_amd(self):
        """
        Get current AM modulation output of the Valon in dB and the frequency modulation
        in hz
        """
        # time.sleep(5)
        msg_db = "amd?\r"
        amd_raw_db = self.send_receive(msg_db)
        # amd_raw_db = amd_raw_db[1]
        msg_f = "amf?\r"
        amd_raw_f = self.send_receive(msg_f)
        # amd_raw_f = amd_raw_f[1]
        # return float(amd_raw_db[4:-3]), float(amd_raw_f[3:-3])*1000
        return amd_raw_db, amd_raw_f

    def stop_amd(self):
        """
        Stop the AM modulation mode
        """

        msg_db = "amd " + str(0) + "\r"
        self.send_receive(msg_db, receive=True)

    def mode_sweep(
        self, start_freq, stop_freq, step, rate, rtime, halt=False, halt_time=100
    ):
        """
        Changes the mode: sweep mode
        - Sweep mode from a STARt frequency (in Mhz) to a STOP frequency (in Mhz)
            with frequency step size (in Mhz), step rate (in ms) and retrace time (ms)
            (retrace time sets a dweel interval of 0ms overt 100 s before to start
            a new sweep )
            halt= stops sweeping
        """

        msg_sweep = "MOD SWE " + "\r"
        self.send_receive(msg_sweep, receive=False)

        msg_start = "START " + str(start_freq) + "\r"
        self.send_receive(msg_start, receive=False)

        msg_stop = "STOP " + str(stop_freq) + "\r"
        self.send_receive(msg_stop, receive=False)

        msg_step = "STEP " + str(step) + "\r"
        self.send_receive(msg_step, receive=False)

        msg_rate = "RATE " + str(rate) + "\r"
        self.send_receive(msg_rate, receive=False)

        msg_rtime = "RTIME " + str(rtime) + "\r"
        self.send_receive(msg_rtime, receive=False)

        t = time.time()

        msg_run = "run " + "\r"
        self.send_receive(msg_run, receive=False)
        if halt:
            while True:
                if time.time() - t > halt_time:
                    msg_halt = "halt " + "\r"
                    self.send_receive(msg_halt, receive=False)
                    msg_modecw = "MOD CW " + "\r"
                    self.send_receive(msg_modecw, receive=False)
                    break
                time.sleep(0.1)

    def close_connection(self, valon_off=False):
        """
        Close connection with a Valon
        Parameters:
        - valon_off: If True, the synthetizer will be turned off before closing the
                     connection
        """

        if valon_off:
            msg = "pdn OFF\r"
            self.send_receive(msg, receive=False)

        self.conn.close()
