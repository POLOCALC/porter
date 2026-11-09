import logging
import board
import adafruit_ina228

logger = logging.getLogger(__name__)

SHUNT_RESISTANCE = 0.02   # ohms
MAX_CURRENT = 5.0         # amps (peak)
I2C_ADDRESS = 0x40        # default address, adjust if A0/A1 are set differently
I2C_BUS = 1               # default I2C bus, extended library allows for bus selection

class INA228:
    def __init__(self, address=I2C_ADDRESS, 
                 shunt_resistance=SHUNT_RESISTANCE, 
                 max_current=MAX_CURRENT,
                 i2c_bus=I2C_BUS):
        self.i2c = None
        self.controller = None
        self.address = address
        self.shunt_resistance = shunt_resistance
        self.max_current = max_current
        self.i2c_bus = i2c_bus

    def open(self):
        try:
            self.i2c = board.I2C()
        except Exception as e:
            logger.error(f"Failed to initialize I2C: {e}")
            raise
        try:
            self.ina228 = adafruit_ina228.INA228(self.i2c, address=self.address)
        except Exception as e:
            logger.error(f"Failed to initialize INA228: {e}")
            raise

    def close(self):
        if self.i2c is not None:
            try:
                self.i2c.deinit()
            except Exception as e:
                logger.error(f"Failed to deinitialize I2C: {e}")
            finally:
                self.i2c = None
        if self.ina228 is not None:
            self.ina228 = None
        return
    
    def read(self):
        if self.ina228 is None:
            logger.error("INA228 not initialized. Call open() first.")
            raise RuntimeError("INA228 not initialized. Call open() first.")
        
        data = {}
        try:
            data['bus_voltage'] = self.ina228.bus_voltage
            data['shunt_voltage'] = self.ina228.shunt_voltage
            data['current'] = self.ina228.current
            data['power'] = self.ina228.power
            data['temperature'] = self.ina228.die_temperature
            data['energy'] = self.ina228.energy
        except Exception as e:
            logger.error(f"Failed to read INA228 data: {e}")
            raise
        return data
    
    def configure(self):
        if self.ina228 is None:
            logger.error("INA228 not initialized. Call open() first.")
            raise RuntimeError("INA228 not initialized. Call open() first.")
        self.ina228.set_calibration(shunt_res=self.shunt_resistance, max_current=self.max_current)
        logger.info(f"INA228 configured with shunt resistance {self.shunt_resistance} ohms and max current {self.max_current} A.")
