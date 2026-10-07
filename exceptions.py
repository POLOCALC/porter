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

class DiskSpaceError(Exception):
    """Not enough free disk space to start or continue acquisition."""
    pass