import os
import lgpio
import time

os.sched_setaffinity(0, {3})

# ADS1015 registers
ADS1015_REG_CONVERSION = 0x00
ADS1015_REG_CONFIG = 0x01

ADS1015_REG_CONFIG_CQUE_NONE = 0x0003
ADS1015_REG_CONFIG_CLAT_NONLAT = 0x0000
ADS1015_REG_CONFIG_CPOL_ACTVLOW = 0x0000
ADS1015_REG_CONFIG_CMODE_TRAD = 0x0000
ADS1015_REG_CONFIG_OS_SINGLE = 0x8000

# ADS1015 config register fields
ADS1015_CONFIG_MUX = {
    "SINGLE_0": 0x4000,
    "SINGLE_1": 0x5000,
    "SINGLE_2": 0x6000,
    "SINGLE_3": 0x7000,
    "DIFF_0_1": 0x0000,
    "DIFF_0_3": 0x1000,
    "DIFF_1_3": 0x2000,
    "DIFF_2_3": 0x3000,
}

ADS1015_CONFIG_GAIN = {
    "2/3": 0x0000,
    "1": 0x0200,
    "2": 0x0400,
    "4": 0x0600,
    "8": 0x0800,
    "16": 0x0A00,
}

ADS1015_VALUE_GAIN = {
    "2/3": 6.144,
    "1": 4.096,
    "2": 2.048,
    "4": 1.024,
    "8": 0.512,
    "16": 0.256,
}

ADS1015_CONFIG_MODE = {"Continuous": 0x0000, "Single-Shot": 0x0100}

ADS1015_CONFIG_RATE = {
    "128": 0x0000,
    "250": 0x0020,
    "490": 0x0040,
    "920": 0x0060,
    "1600": 0x0080,
    "2400": 0x00A0,
    "3300": 0x00C0,
}

# Open the bus
bus = lgpio.i2c_open(6, 0x48)

rate = 128 
total_time = 1800

# Write the config
config_register = (
    ADS1015_REG_CONFIG_CQUE_NONE
    | ADS1015_REG_CONFIG_CLAT_NONLAT
    | ADS1015_REG_CONFIG_CPOL_ACTVLOW
    | ADS1015_REG_CONFIG_CMODE_TRAD
    | ADS1015_CONFIG_MODE["Continuous"]
    | ADS1015_CONFIG_RATE["1600"]
    | ADS1015_CONFIG_GAIN["8"]
    | ADS1015_CONFIG_MUX["DIFF_0_1"]
    | ADS1015_REG_CONFIG_OS_SINGLE
)
config_bytes = [
    (config_register >> 8) & 0xFF,
    config_register & 0xFF,
]
lgpio.i2c_write_i2c_block_data(bus, ADS1015_REG_CONFIG, config_bytes)

time.sleep(1)

t_prev = 0
num_samples = 0
start_time = time.perf_counter()
timing_results = ""
next_time = time.time()
period = 1.0 / rate
index = 0;

while time.perf_counter() - start_time < total_time:
    sleep_time = next_time - time.time() 
    death = 0
    while sleep_time < 0:
        next_time += period
        sleep_time = next_time - time.time()
        death += 1
        print(f"{index}: {death}")
    time.sleep(sleep_time)
    next_time += period

    t_start = time.perf_counter_ns()
    _, raw_value = lgpio.i2c_read_i2c_block_data(
        bus, ADS1015_REG_CONVERSION, 2
    )
    t = time.time()
    read_time = time.perf_counter_ns() - t_start
    num_samples += 1
    timing_results += f"{t} {raw_value} {read_time / 1e6} {death}\n" 
    index += 1

with open('with_values.txt', 'w') as f:
    f.write(timing_results)

print(f"Read {num_samples} samples in {total_time} seconds")
