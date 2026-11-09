import RPi.GPIO as GPIO
import time
import os

PIN = 19
freq = 10
duty_cycle = 500000

os.sched_setaffinity(0, {2})

GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN, GPIO.OUT)

t_start = time.time_ns()

edges = []
levels = []

try:
    while True:
        #edges.append(time.time_ns())
        GPIO.output(PIN, GPIO.HIGH)
        edges.append(time.time_ns())
        levels.append(3.3)
        time.sleep(0.5 * (1 / freq))
        #edges.append(time.time_ns())
        GPIO.output(PIN, GPIO.LOW)
        edges.append(time.time_ns())
        levels.append(0)
        time.sleep(0.5 * (1 / freq))
except KeyboardInterrupt:
    GPIO.cleanup()

with open("ads_sqwav_results.txt", 'w') as f:
    for edge, level in zip(edges, levels):
        f.write(f"{int(edge)} {int(level)}\n")
