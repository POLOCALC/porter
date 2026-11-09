import serial
import pyubx2 as ubx
import time

cfg = [
    ('CFG_UART2OUTPROT_NMEA', 1),
    ('CFG_UART2OUTPROT_UBX', 0),
    ('CFG_UART2OUTPROT_RTCM3X', 0),
    ('CFG_UART1OUTPROT_UBX', 1),
    ('CFG_UART1OUTPROT_RTCM3X', 0),
    ('CFG_UART1OUTPROT_NMEA', 0),
    ('CFG_MSGOUT_NMEA_ID_DTM_UART2', 1),
    ('CFG_MSGOUT_NMEA_ID_GGA_UART2', 1),
    ('CFG_MSGOUT_NMEA_ID_GLL_UART2', 1),
    ('CFG_MSGOUT_NMEA_ID_GSA_UART2', 1),
    ('CFG_MSGOUT_NMEA_ID_GST_UART2', 1),
    ('CFG_MSGOUT_NMEA_ID_GSV_UART2', 1),
    ('CFG_MSGOUT_UBX_NAV_POSLLH_UART1', 1),
    ('CFG_MSGOUT_UBX_RXM_RAWX_UART1',1),
    ('CFG_MSGOUT_UBX_RXM_SFRBX_UART1', 1)
]

conn = serial.Serial('/dev/ttyAMA0', 38400, timeout=2)
cfg_msg = ubx.UBXMessage.config_set(1, 0, cfg)
conn.write(cfg_msg.serialize())
time.sleep(0.5)
conn.close()




