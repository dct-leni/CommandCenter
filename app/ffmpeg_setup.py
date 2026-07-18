"""
FFmpeg and MediaMTX path helpers.
Binaries are expected in bin/ folder — run setup_binaries.bat once to download them.
"""

from pathlib import Path
import subprocess

BIN_DIR = Path(__file__).parent.parent / "bin"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"
FFPROBE_EXE = BIN_DIR / "ffprobe.exe"
MEDIAMTX_EXE = BIN_DIR / "mediamtx.exe"

_NVENC_AVAILABLE = None


def is_ffmpeg_installed() -> bool:
    """Check if portable FFmpeg is available in bin/."""
    return FFMPEG_EXE.exists() and FFPROBE_EXE.exists()


def is_mediamtx_installed() -> bool:
    """Check if portable MediaMTX is available in bin/."""
    return MEDIAMTX_EXE.exists()


def is_nvenc_available() -> bool:
    """Check if hardware NVENC encoding (`h264_nvenc`) is supported on this machine."""
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE

    if not is_ffmpeg_installed():
        _NVENC_AVAILABLE = False
        return _NVENC_AVAILABLE

    try:
        res = subprocess.run(
            [
                str(FFMPEG_EXE),
                "-v", "error",
                "-f", "lavfi",
                "-i", "nullsrc=s=640x360:d=0.05",
                "-c:v", "h264_nvenc",
                "-f", "null",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        _NVENC_AVAILABLE = (res.returncode == 0)
    except Exception:
        _NVENC_AVAILABLE = False

    return _NVENC_AVAILABLE


def get_ffmpeg_path() -> str:
    """Return the path to the FFmpeg executable."""
    return str(FFMPEG_EXE)


def get_ffprobe_path() -> str:
    """Return the path to the FFprobe executable."""
    return str(FFPROBE_EXE)


def get_mediamtx_path() -> str:
    """Return the path to the MediaMTX executable."""
    return str(MEDIAMTX_EXE)


def get_binaries_status() -> dict:
    """Return availability status of all binaries."""
    return {
        "ffmpeg": is_ffmpeg_installed(),
        "mediamtx": is_mediamtx_installed(),
        "ffmpeg_path": str(FFMPEG_EXE),
        "mediamtx_path": str(MEDIAMTX_EXE),
        "nvenc_available": is_nvenc_available(),
    }

