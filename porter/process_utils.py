import atexit
import logging
import os
import signal 
import subprocess
import time

logger = logging.getLogger(__name__)
# every binary started through start_process()
_children = set()

def start_process(cmd, **kwargs):
    proc = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid, **kwargs)
    _children.add(proc)
    return proc

def stop_process(proc, name="", first_signal=signal.SIGTERM, grace=3.0):
    """Signal the child's process group, wait, escalate to SIGKILL. Safe to call twice."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            os.killpg(proc.pid, first_signal) # pgid == pid because of setsid
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                logger.warning(f"{name}: no exit after {first_signal.name}, sending SIGKILL")
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
    except ProcessLookupError:
        # already gone
        pass
    except Exception as e:
        logger.error(f"{name}: failed to stop child process: {e}")
    finally:
        _children.discard(proc)

def stop_all(grace=3.0):
    """Last line of defence: stop anything still registered, within one shared deadline."""
    procs = [p for p in list(_children) if p.poll() is None]
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    _children.clear()


atexit.register(stop_all)
