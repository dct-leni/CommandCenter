"""
Process management utilities for CommandCenter.
Handles safe, recursive process tree termination across Windows and POSIX systems.
"""

import os
import signal
import subprocess
import logging
from typing import Union

logger = logging.getLogger(__name__)


def terminate_process_tree(target: Union[int, subprocess.Popen], timeout: float = 2.0) -> None:
    """
    Safely terminate a process and all its child processes.
    
    Attempts graceful termination first, falling back to force-kill if the process
    does not exit within the timeout period.
    """
    if target is None:
        return

    pid = target.pid if isinstance(target, subprocess.Popen) else int(target)
    if pid <= 0:
        return

    # Don't kill ourselves by accident unless specifically asked
    if pid == os.getpid():
        logger.debug("terminate_process_tree called on self, skipping tree kill")
        return

    if os.name == "nt":
        # Windows: Use taskkill /F /T to recursively kill the entire process tree
        if isinstance(target, subprocess.Popen) and target.poll() is None:
            try:
                target.terminate()
                target.wait(timeout=timeout)
                return
            except (subprocess.TimeoutExpired, Exception):
                pass

        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            logger.debug(f"taskkill failed for PID {pid}: {e}")
    else:
        # POSIX: Send SIGTERM to process group, fallback to SIGKILL
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                return

        # Check if process exited, otherwise SIGKILL
        if isinstance(target, subprocess.Popen):
            try:
                target.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                pass

        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
