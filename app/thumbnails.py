"""
Thumbnail generation from video files using FFmpeg.
Caches thumbnails in the thumbnails/ directory.
"""

import hashlib
import os
import subprocess
import logging
from pathlib import Path
from typing import Optional

from app.ffmpeg_setup import get_ffmpeg_path, is_ffmpeg_installed

logger = logging.getLogger(__name__)

THUMBNAILS_DIR = Path(__file__).parent.parent / "thumbnails"


def _get_cache_key(video_path: str) -> str:
    """Generate a cache key based on file path and modification time."""
    stat = os.stat(video_path)
    key_str = f"{video_path}:{stat.st_mtime}:{stat.st_size}"
    return hashlib.md5(key_str.encode()).hexdigest()


def get_thumbnail_path(video_path: str) -> Path:
    """Get the cached thumbnail path for a video file."""
    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = _get_cache_key(video_path)
    return THUMBNAILS_DIR / f"{cache_key}.jpg"


def generate_thumbnail(video_path: str, force: bool = False) -> Optional[str]:
    """
    Generate a thumbnail from a video file.
    Returns the path to the thumbnail image, or None on failure.
    """
    if not is_ffmpeg_installed():
        logger.warning("FFmpeg not installed, cannot generate thumbnail")
        return None

    thumb_path = get_thumbnail_path(video_path)

    # Return cached version if it exists
    if thumb_path.exists() and not force:
        return str(thumb_path)

    try:
        cmd = [
            get_ffmpeg_path(),
            "-i", video_path,
            "-ss", "00:01:30",       # Seek to 3 seconds
            "-vframes", "1",          # Extract 1 frame
            "-q:v", "6",              # JPEG quality (2=best, 31=worst)
            "-vf", "scale=320:-1",    # Scale to 320px width, keep aspect ratio
            "-y",                     # Overwrite output
            str(thumb_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        if thumb_path.exists():
            return str(thumb_path)
        else:
            # If seek failed (short video), try from start
            cmd[cmd.index("00:01:30")] = "00:00:00"
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if thumb_path.exists():
                return str(thumb_path)

            logger.error(f"Thumbnail generation failed for {video_path}: {result.stderr[-500:]}")
            return None

    except subprocess.TimeoutExpired:
        logger.error(f"Thumbnail generation timed out for {video_path}")
        return None
    except Exception as e:
        logger.error(f"Thumbnail generation error for {video_path}: {e}")
        return None


def get_video_metadata(video_path: str) -> dict:
    """Get video file metadata using ffprobe."""
    from app.ffmpeg_setup import get_ffprobe_path, is_ffmpeg_installed

    if not is_ffmpeg_installed():
        return {"error": "FFmpeg not installed"}

    try:
        cmd = [
            get_ffprobe_path(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            video_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            fmt = data.get("format", {})
            video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})

            return {
                "duration": float(fmt.get("duration", 0)),
                "size": int(fmt.get("size", 0)),
                "bitrate": int(fmt.get("bit_rate", 0)),
                "codec": video_stream.get("codec_name", "unknown"),
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "fps": _parse_fps(video_stream.get("r_frame_rate", "0/1")),
            }
        else:
            return {"error": result.stderr[-200:]}

    except Exception as e:
        return {"error": str(e)}


def _parse_fps(fps_str: str) -> float:
    """Parse FFprobe frame rate string like '30/1' to float."""
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return round(int(num) / max(int(den), 1), 2)
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 0.0
