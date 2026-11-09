from signal import SIGINT, SIGTERM

LOGGING_FORMAT_FILE = "%(asctime)s.%(msecs)03d [%(levelname)s]\t (%(name)s) - %(message)s"
#LOGGING_FORMAT_STREAM = AnsiColorFormatter('[%(asctime)s.%(msecs)03d - %(levelname)s] name: %(name)s thread: %(threadName)s: %(message)s')
LOGGING_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOGGING_LEVEL = "INFO"


# generic attempts for retrying operations (e.g. reading from a sensor)
ATTEMPTS = 10 

# define signal to catch
signal_to_catch = [SIGINT, SIGTERM]

SHUTDOWN_TIMEOUT = 12  # seconds for all threads to finish; keep below TimeoutStopSec=20 in porter@.service
SENSOR_INIT_TIMEOUT = 15  # seconds to wait for a sensor thread to finish connecting/configuring
STATUS_WRITER_UPDATE_RATE = 1  # Hz, how often the status writer writes to disk

# define paths and file/folder naming
INCREMENTAL_FILE_PREFIX = True  # whether to add an incremental prefix to the data folder
home_directory       = "/home/polocalc"
data_folder_name     = "data"
sensors_folder_name  = "sensors_data"
camera_folder_name   = "camera_data"
current_symlink_name = "current"
logfile_name         = "flight.log"