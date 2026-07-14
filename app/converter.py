"""
Video file converter — converts various video formats to MPEG-TS (.ts).
Tracks conversion progress and manages file renaming.
"""

import asyncio
import os
import re
import subprocess
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum

from app.ffmpeg_setup import get_ffmpeg_path, is_ffmpeg_installed
from app.thumbnails import generate_thumbnail, get_video_metadata

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg"}


class ConversionStatus(str, Enum):
    PENDING = "pending"
    CONVERTING = "converting"
    DONE = "done"
    ERROR = "error"


@dataclass
class FileInfo:
    filename: str
    filepath: str
    size: int
    extension: str
    status: ConversionStatus = ConversionStatus.PENDING
    progress: float = 0.0
    error: str = ""
    ts_filename: str = ""
    metadata: dict = field(default_factory=dict)
    thumbnail: Optional[str] = None


class Converter:
    """Manages video-to-TS conversion for a folder."""

    def __init__(self):
        self.files: Dict[str, FileInfo] = {}
        self.source_folder: str = ""
        self._active_processes: Dict[str, subprocess.Popen] = {}
        self._conversion_tasks: Dict[str, asyncio.Task] = {}

    def scan_folder(self, folder_path: str) -> List[dict]:
        """Scan a folder for video files and return their info."""
        self.source_folder = folder_path
        folder = Path(folder_path)

        if not folder.exists() or not folder.is_dir():
            return []

        old_files = self.files.copy()
        self.files.clear()
        results = []

        for f in sorted(folder.iterdir()):
            if not f.is_file():
                continue

            ext = f.suffix.lower()

            # Check if this is an already-converted .ts file
            if ext == ".ts":
                info = FileInfo(
                    filename=f.name,
                    filepath=str(f),
                    size=f.stat().st_size,
                    extension=ext,
                    status=ConversionStatus.DONE,
                    progress=1.0,
                    ts_filename=f.name,
                )
                # Generate thumbnail for .ts files too
                thumb = generate_thumbnail(str(f))
                if thumb:
                    info.thumbnail = thumb
                self.files[f.name] = info
                results.append(self._file_to_dict(info))
                continue

            # Check for convertible video files
            if ext in VIDEO_EXTENSIONS:
                # Preserve state for actively converting or failed files
                if f.name in old_files and old_files[f.name].status in (ConversionStatus.CONVERTING, ConversionStatus.ERROR):
                    info = old_files[f.name]
                    self.files[f.name] = info
                    results.append(self._file_to_dict(info))
                    continue

                ts_name = f.stem + ".ts"
                ts_path = folder / ts_name

                # Check if already converted
                if ts_path.exists():
                    status = ConversionStatus.DONE
                    progress = 1.0
                else:
                    status = ConversionStatus.PENDING
                    progress = 0.0

                info = FileInfo(
                    filename=f.name,
                    filepath=str(f),
                    size=f.stat().st_size,
                    extension=ext,
                    status=status,
                    progress=progress,
                    ts_filename=ts_name,
                )

                # Generate thumbnail
                thumb = generate_thumbnail(str(f))
                if thumb:
                    info.thumbnail = thumb

                # Get metadata
                info.metadata = get_video_metadata(str(f))

                self.files[f.name] = info
                results.append(self._file_to_dict(info))

        return results

    async def convert_file(self, filename: str) -> bool:
        """Start converting a single file to .ts format."""
        if not is_ffmpeg_installed():
            logger.error("FFmpeg not installed")
            return False

        if filename not in self.files:
            logger.error(f"File not found: {filename}")
            return False

        info = self.files[filename]
        if info.status == ConversionStatus.DONE:
            return True
        if info.status == ConversionStatus.CONVERTING:
            return True  # Already in progress

        info.status = ConversionStatus.CONVERTING
        info.progress = 0.0
        info.error = ""

        # Start conversion in background
        task = asyncio.create_task(self._run_conversion(filename))
        self._conversion_tasks[filename] = task
        return True

    async def convert_all(self) -> int:
        """Start converting all pending files. Returns count of started conversions."""
        count = 0
        for filename, info in self.files.items():
            if info.status == ConversionStatus.PENDING:
                await self.convert_file(filename)
                count += 1
        return count

    async def _run_conversion(self, filename: str):
        """Run the actual FFmpeg conversion process."""
        info = self.files[filename]
        input_path = info.filepath
        output_path = str(Path(input_path).parent / info.ts_filename)

        try:
            # First attempt: copy codecs (fast, no re-encoding, flawless original quality)
            success = await self._ffmpeg_convert(input_path, output_path, filename, strategy="copy")

            if not success:
                # Second attempt: Hardware re-encode (NVENC)
                logger.info(f"Codec copy failed for {filename}, re-encoding with NVENC...")
                info.progress = 0.0
                success = await self._ffmpeg_convert(input_path, output_path, filename, strategy="nvenc")
            
            if not success:
                # Third attempt: Software re-encode (CPU) - Fallback if no Nvidia GPU is present
                logger.info(f"NVENC failed for {filename}, re-encoding with CPU (libx264)...")
                info.progress = 0.0
                success = await self._ffmpeg_convert(input_path, output_path, filename, strategy="cpu")

            if success:
                info.status = ConversionStatus.DONE
                info.progress = 1.0
                # Move original file to 'original' subfolder
                try:
                    import shutil
                    original_folder = Path(input_path).parent / "original"
                    original_folder.mkdir(exist_ok=True)
                    original_path = original_folder / Path(input_path).name
                    shutil.move(str(input_path), str(original_path))
                    info.filepath = str(original_path)
                except Exception as e:
                    logger.warning(f"Could not move original file: {e}")

                # Generate thumbnail for the new .ts file
                thumb = generate_thumbnail(output_path)
                if thumb:
                    info.thumbnail = thumb

                logger.info(f"Conversion complete: {filename} → {info.ts_filename}")
            else:
                info.status = ConversionStatus.ERROR
                # Clean up partial output
                Path(output_path).unlink(missing_ok=True)

        except Exception as e:
            info.status = ConversionStatus.ERROR
            info.error = str(e)
            logger.error(f"Conversion failed for {filename}: {e}")
        finally:
            self._conversion_tasks.pop(filename, None)
            self._active_processes.pop(filename, None)
            self.scan_folder(self.source_folder)

    async def _ffmpeg_convert(self, input_path: str, output_path: str, filename: str, strategy: str) -> bool:
        """Run FFmpeg conversion and track progress."""
        info = self.files[filename]

        # Get duration for progress calculation
        duration = info.metadata.get("duration", 0)

        base_cmd = [get_ffmpeg_path(), "-y", "-hwaccel", "auto", "-i", input_path]
        
        if strategy == "copy":
            cmd = base_cmd + [
                "-c:v", "copy",
                "-c:a", "copy",
                "-bsf:v", "h264_mp4toannexb",
                "-f", "mpegts",
                output_path,
            ]
        elif strategy == "nvenc":
            cmd = base_cmd + [
                "-c:v", "h264_nvenc",
                "-preset", "p6",           # Higher quality preset (slower/better than p4)
                "-tune", "hq",             # High-quality tuning profile
                "-b:v", "6M",              # Explicit high video bitrate
                "-maxrate", "8M",          # Prevent bitrate spikes for stream stability
                "-bufsize", "16M",         # VBR buffer
                "-g", "60",                # Force keyframe every 2 seconds (crucial for RTMP)
                "-c:a", "aac",
                "-b:a", "320k",            # Higher audio bitrate
                "-ac", "2",                # Force stereo audio
                "-f", "mpegts",
                output_path,
            ]
        else:
            # CPU Fallback (libx264)
            cmd = base_cmd + [
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "21",              # Constant Rate Factor for consistent high quality
                "-maxrate", "8M",
                "-bufsize", "16M",
                "-g", "60",
                "-c:a", "aac",
                "-b:a", "320k",
                "-ac", "2",
                "-f", "mpegts",
                output_path,
            ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            self._active_processes[filename] = process

            # Parse progress from stderr using chunks to fix the \r block issue
            stderr_data = []
            buffer = ""
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                
                buffer += chunk.decode("utf-8", errors="replace")
                
                if '\r' in buffer or '\n' in buffer:
                    lines = buffer.replace('\r', '\n').split('\n')
                    buffer = lines.pop()  # Keep the last incomplete part
                    
                    for line in lines:
                        if line.strip():
                            stderr_data.append(line)
                        # Extract time from FFmpeg output
                        match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                        if match and duration > 0:
                            current_time = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                            info.progress = min(current_time / duration, 0.99)

            await process.wait()

            if process.returncode == 0 and Path(output_path).exists():
                return True
            else:
                stderr_text = "".join(stderr_data)
                info.error = stderr_text[-300:]
                return False

        except Exception as e:
            info.error = str(e)
            return False

    def get_status(self) -> List[dict]:
        """Get conversion status for all files."""
        return [self._file_to_dict(info) for info in self.files.values()]

    def _file_to_dict(self, info: FileInfo) -> dict:
        """Convert FileInfo to a JSON-serializable dict."""
        return {
            "filename": info.filename,
            "filepath": info.filepath,
            "size": info.size,
            "extension": info.extension,
            "status": info.status.value,
            "progress": round(info.progress, 3),
            "error": info.error,
            "ts_filename": info.ts_filename,
            "metadata": info.metadata,
            "has_thumbnail": info.thumbnail is not None,
        }


# Singleton converter instance
converter = Converter()
