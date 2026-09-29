from digi.xbee.devices import XBeeDevice
from digi.xbee.devices import RemoteXBeeDevice
from digi.xbee.serial import XBeeSerialPort
from digi.xbee.models.address import XBee64BitAddress
from digi.xbee.exception import TimeoutException, TransmitException
from digi.xbee.models.options import TransmitOptions

IDs = {
    "OBI": "0013A20041C2BB3E",
    "LUKE": "0013A20041C2B524"
}

POWER_LEVEL = 4  # 4 is the maximum (+5dBm)
MAX_PACKET_SIZE = 80
END_OF_MESSAGE_BYTE = b'\x00'

class Xbee:
    '''
    Class to configure parameters of a xbee antenna
    '''

    def __init__(self, port, baudrate=9600):
        self.port = port
        self.baudrate = baudrate
        self.device = XBeeDevice(self.port, self.baudrate)

    def change_baudrate(self, new_baudrate):
        xbee_ser = XBeeSerialPort(self.baudrate, self.port)
        xbee_ser.set_baudrate(new_baudrate)
        self.baudrate = new_baudrate
        self.device = XBeeDevice(self.port, self.baudrate)

    def set_param(self, parameter, value):
        self.device.set_parameter(parameter, bytearray([value]))

    def set_role(self, role):
        available_roles = ['join', 'form']
        if role in available_roles:
            if role == 'join':
                value = 0
            elif role == 'form':
                value = 1
            self.set_param('CE', value)

    def open(self, force_settings=False, remote_name=None):
        self.remote_device_id = IDs.get(remote_name, None) if remote_name is not None else None
        self.device.open(force_settings=force_settings)
        # try to discover the remote device if remote_device_id is not provided
        if self.remote_device_id is not None:
            addr_64bit = XBee64BitAddress.from_hex_string(self.remote_device_id)
            self.remote_device = RemoteXBeeDevice(self.device, addr_64bit)
        else:
            self.remote_device = None

        # set maxium power level
        self.set_param("PL", POWER_LEVEL)

    def close(self):
        self.device.close()
    
    def send_msg(self, msg, ack=True):
        if isinstance(msg, list):
            msg = ''.join(msg)
        if isinstance(msg, str):
            msg = msg.encode('utf-8')
        # append EOM before chunking so the last chunk always carries it
        msg = msg + END_OF_MESSAGE_BYTE
        chunks = [msg[i:i+MAX_PACKET_SIZE] for i in range(0, len(msg), MAX_PACKET_SIZE)]
        opts = TransmitOptions.NONE.value if ack else TransmitOptions.DISABLE_ACK.value
        for chunk in chunks:
            self.device.send_data(self.remote_device, chunk, transmit_options=opts)


    def send_msg_broadcast(self, msg):
        if isinstance(msg, list):
            msg = ''.join(msg)
        if isinstance(msg, str):
            msg = msg.encode('utf-8')
        # append EOM before chunking so the last chunk always carries it
        msg = msg + END_OF_MESSAGE_BYTE
        chunks = [msg[i:i+MAX_PACKET_SIZE] for i in range(0, len(msg), MAX_PACKET_SIZE)]
        for chunk in chunks:
            self.device.send_data_broadcast(chunk)
    
    def poll_msg(self, timeout=1):
        msg = b''
        while True:
            try:
                temp = self.device.read_data(timeout=timeout)
            except TimeoutException:
                continue
            if temp is not None:
                msg += temp.data
                if msg.endswith(END_OF_MESSAGE_BYTE):
                    msg = msg[:-1]  # remove the end of message byte
                    sender = temp.remote_device.get_64bit_addr()
                    return sender, msg
                # if message is too long, return it anyway
                if len(msg) > MAX_PACKET_SIZE*5:
                    sender = temp.remote_device.get_64bit_addr()
                    return sender, msg

    def poll_msg_debug(self, timeout=1):
        try:
            temp = self.device.read_data(timeout=timeout)
            if temp is not None:
                print(f"RAW: {temp.data}")
        except TimeoutException:
            pass