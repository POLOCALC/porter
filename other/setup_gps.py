import datetime
import logging
import os
import queue
import signal
import sys
import threading
import time

import yaml

import porter.sensors.sensors_handler as sh
import porter.threads as threads
import porter.valon as valon

path = os.path.dirname(os.path.realpath(__file__))
home_dir = os.environ["HOME"]

date = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

if not os.path.exists(home_dir + "/data"):
    os.mkdir(home_dir + "/data")
if not os.path.exists(path + "/data/" + date + '_gps' ):
    os.mkdir(home_dir + "/data/" + date + '_gps' )

logging.basicConfig(
    filename=home_dir + "/data/" + date + "_gps/file.log",
    filemode="w",
    format="%(asctime)s  [%(threadName)s]  %(levelname)s:%(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
    level=logging.DEBUG,
)

logger = logging.getLogger("mainlogger")

try:
    from sour_core import sony
except ModuleNotFoundError:
    pass

"""
Classes and Function to deal with interrupting the code
"""


class ServiceExitError(Exception):
    """
    Custom exception which is used to trigger the clean exit
    of all running threads and the main program.
    """

    pass


class FlagSetError(Exception):

    pass


def handler(signum, frame):
    print("Signal Sent")
    logger.info(f"Caught signal {signal.strsignal(signum)}")
    raise ServiceExitError


def capture_flag(flag):
    logger.info(f"Flag has been set in a Thread")
    if flag.is_set():
        raise FlagSetError


signal_to_catch = [
    signal.SIGINT,
    signal.SIGTERM,
]


def main():
    cfg_name = sys.argv[1]

    flag = threading.Event()

    cfg_path = path + "/" + cfg_name

    status = True

    with open(cfg_path, "r") as cfg:
        config = yaml.safe_load(cfg)
        logging.info(f"Loaded configuration {cfg_name}")

    if not os.path.exists(home_dir + "/data/" + date + "_gps/sensors_data"):
        os.mkdir(home_dir + "/data/" + date + "_gps/sensors_data")
        sensor_path = home_dir + "/data/" + date + "_gps/sensors_data/"

    for sig in signal_to_catch:
        signal.signal(sig, handler)

    time.sleep(2)

    if "sensors" in config.keys():
	    sensor_locks = {}
	    sensor_names = {}
	    sensor_connections = {}

	    for i in config["sensors"].keys():
		    sensors_handler = sh.Handler(
			    config["sensors"][i], local=config["local_development"]
		    )

		    name = config["sensors"][i]["name"]

		    sensor_connections[name] = sensors_handler.obj
		    sensor_locks[name] = threading.Lock()
		    sensor_names[name] = name

	    for i in sensor_connections.keys():
		    threads.Sensors(
			    conn=sensor_connections[i],
			    sensor_lock=sensor_locks[i],
			    flag=flag,
			    date=date,
			    path=sensor_path,
			    sensor_name=sensor_names[i],
			    daemon=False,
		    ).start()
		    
	    time.sleep(1)
	    
	    flag.set()
		
if __name__ == "__main__":
    main()	
