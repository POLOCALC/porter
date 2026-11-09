import pigpio
import time
import os

pin = 19
freq = 10
duty_cycle = 500000

os.sched_setaffinity(0, {2})

gpio = pigpio.pi()

rising_edges = []
levels = []


def rising_edge_callback(gpio, level, tick):
    ts = time.time_ns()
    rising_edges.append(ts)
    levels.append(level)

cb = gpio.callback(pin, pigpio.RISING_EDGE, rising_edge_callback)
ts = time.time() * 1e6
gpio.hardware_PWM(pin, freq, duty_cycle)
print(ts)



try:
    time.sleep(60)
except:
    gpio.hardware_PWM(pin, 0, 0)
    cb.cancel()
    gpio.stop()

with open("ads_sqwav_results.txt", 'w') as f:
    for edge, level in zip(rising_edges, levels):
        f.write(f"{int(edge)} {int(level)}\n")
