"""
FFmpeg and MediaMTX path helpers.
Binaries are expected in bin/ folder — run setup_binaries.bat once to download them.
"""

from pathlib import Path

BIN_DIR = Path(__file__).parent.parent / "bin"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"
FFPROBE_EXE = BIN_DIR / "ffprobe.exe"
MEDIAMTX_EXE = BIN_DIR / "mediamtx.exe"


def is_ffmpeg_installed() -> bool:
    """Check if portable FFmpeg is available in bin/."""
    return FFMPEG_EXE.exists() and FFPROBE_EXE.exists()


def is_mediamtx_installed() -> bool:
    """Check if portable MediaMTX is available in bin/."""
    return MEDIAMTX_EXE.exists()


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
    }
