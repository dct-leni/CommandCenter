"""
Process Loopback Audio Capture Module for CommandCenter.

Captures isolated PCM audio directly from target Firefox process trees
using native Windows Core Audio AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK via bin/app_loopback.exe.
100% process-isolated with zero virtual cables and zero speaker leakage.
"""

import os
import sys
import time
import subprocess
import threading
import logging
from pathlib import Path
from typing import Dict, Optional, Union, Any

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_APP_LOOPBACK_EXE = _BASE_DIR / "bin" / "app_loopback.exe"


class ProcessLoopbackAudioCapture:
    """
    Spawns and manages bin/app_loopback.exe for a specific target browser PID tree
    and feeds continuous 16-bit 48kHz Stereo PCM audio directly into FFmpeg stdin.
    """
    def __init__(self, stream_id: str, target_pid: int, pipe_fd: Union[int, Any]):
        self.stream_id = stream_id
        self.target_pid = target_pid
        self.pipe_fd = pipe_fd
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not _APP_LOOPBACK_EXE.exists():
            logger.error(f"[{self.stream_id}] app_loopback.exe not found at {_APP_LOOPBACK_EXE}")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, name=f"app_loopback_{self.stream_id}", daemon=True)
        self._thread.start()

    def _run(self):
        logger.info(f"[{self.stream_id}] Starting app_loopback.exe for PID {self.target_pid} (48000Hz PCM)")
        try:
            self._proc = subprocess.Popen(
                [str(_APP_LOOPBACK_EXE), str(self.target_pid), "48000"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )

            # Background thread to log stderr from app_loopback.exe
            def _log_stderr():
                try:
                    for line in iter(self._proc.stderr.readline, b""):
                        line_str = line.decode(errors="replace").strip()
                        if line_str:
                            logger.info(f"[{self.stream_id}] [app_loopback] {line_str}")
                except Exception:
                    pass
            threading.Thread(target=_log_stderr, daemon=True).start()

            # Stream stdout chunks into FFmpeg pipe_fd
            is_fd_int = isinstance(self.pipe_fd, int)
            raw_stdout = getattr(self._proc.stdout, "raw", self._proc.stdout)

            while self._running and self._proc.poll() is None:
                data = raw_stdout.read(4096)
                if not data:
                    break
                if is_fd_int:
                    os.write(self.pipe_fd, data)
                else:
                    self.pipe_fd.write(data)
                    self.pipe_fd.flush()

        except (BrokenPipeError, OSError) as e:
            logger.debug(f"[{self.stream_id}] Audio pipe closed: {e}")
        except Exception as e:
            logger.error(f"[{self.stream_id}] Audio capture worker error: {e}", exc_info=True)
        finally:
            self.stop()

    def stop(self):
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        logger.info(f"[{self.stream_id}] Stopped app_loopback audio capture for PID {self.target_pid}")


_ACTIVE_CAPTURE_THREADS: Dict[str, ProcessLoopbackAudioCapture] = {}
_CAPTURE_LOCK = threading.Lock()


def start_process_audio_capture(stream_id: str, target_pid: int, pipe_fd: Union[int, Any]) -> Optional[ProcessLoopbackAudioCapture]:
    """Start and register an isolated PROCESS_LOOPBACK capture thread for a stream."""
    stop_process_audio_capture(stream_id)
    with _CAPTURE_LOCK:
        thread = ProcessLoopbackAudioCapture(stream_id, target_pid, pipe_fd)
        thread.start()
        _ACTIVE_CAPTURE_THREADS[stream_id] = thread
        return thread


def stop_process_audio_capture(stream_id: str):
    """Stop and unregister an active PROCESS_LOOPBACK capture thread."""
    with _CAPTURE_LOCK:
        thread = _ACTIVE_CAPTURE_THREADS.pop(stream_id, None)
        if thread:
            thread.stop()
