"""
Video file converter — converts various video formats to MPEG-TS (.ts).
Tracks conversion progress and manages file renaming.

Behavior added on top of the original converter:
  - Only One audio tracks are kept (language tag "tur"/"tr"). If no language track is found, the first
    audio stream is kept as a safe fallback (and this is logged).
  - Subtitle streams are always dropped (we simply never -map them).
  - Video is capped at "HD" (longest side <= 1920px). If the source is
    already HD or smaller, we do a pure stream copy (fast, lossless).
    If the source is above HD (2K/4K/etc.) we must re-encode to scale
    it down, so the copy strategy is skipped and we go straight to
    NVENC (falling back to CPU/libx264 if NVENC isn't available).
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum
 
from app.ffmpeg_setup import get_ffmpeg_path, is_ffmpeg_installed
from app.thumbnails import generate_thumbnail, get_video_metadata
from app.config import load_config
 
logger = logging.getLogger(__name__)
 
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg"}
 
# Longest side (width or height) allowed. 1920 == standard "HD" (1080p landscape).
MAX_HD_DIMENSION = 1920
 
 
class ConversionStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
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
    # Populated once we probe the file, right before conversion.
    audio_note: str = ""       # human-readable note about which audio track(s) were kept
    scaled_note: str = ""      # human-readable note about whether/how video was scaled
 
 
def _get_ffprobe_path() -> str:
    """Best-effort resolution of the ffprobe binary that ships alongside ffmpeg."""
    ffmpeg_path = get_ffmpeg_path()
    p = Path(ffmpeg_path)
    candidate_name = "ffprobe.exe" if p.suffix.lower() == ".exe" else "ffprobe"
    candidate = p.parent / candidate_name
    if candidate.exists():
        return str(candidate)
    # Fall back to a plain name replace, then to just "ffprobe" on PATH.
    if "ffmpeg" in p.name.lower():
        guess = p.parent / p.name.lower().replace("ffmpeg", "ffprobe")
        if guess.exists():
            return str(guess)
    return "ffprobe"
 
 
async def probe_streams(input_path: str) -> dict:
    """
    Run ffprobe and return a dict:
      {
        "video": {"index": int, "width": int, "height": int} | None,
        "audio": [{"index": int, "language": str, "title": str}, ...],
      }
    """
    ffprobe = _get_ffprobe_path()
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        input_path,
    ]
    result = {"video": None, "audio": []}
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, _ = await process.communicate()
        data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except Exception as e:
        logger.warning(f"ffprobe failed for {input_path}: {e}")
        return result
 
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        idx = stream.get("index")
        if codec_type == "video" and result["video"] is None:
            result["video"] = {
                "index": idx,
                "width": stream.get("width", 0),
                "height": stream.get("height", 0),
            }
        elif codec_type == "audio":
            tags = stream.get("tags", {}) or {}
            language = (tags.get("language") or "").lower()
            title = (tags.get("title") or "").lower()
            result["audio"].append({"index": idx, "language": language, "title": title})
 
    return result
 
 
def _select_audio_by_language(audio_streams: List[dict], languages: List[str]) -> (List[int], str):
    """
    Return (list of absolute stream indices to keep, human-readable note).
    `languages` is the configured list of language tags to keep (e.g.
    ["tur", "tr", "trk"]), matched against each stream's language tag or,
    as a loose fallback, checked as a substring of the stream title.
    """
    wanted = {lang.lower() for lang in languages}
 
    matched = [
        a for a in audio_streams
        if a["language"] in wanted or any(lang in a["title"] for lang in wanted if lang)
    ]
    if matched:
        return [a["index"] for a in matched], f"kept {len(matched)} audio track(s) matching {sorted(wanted)}"
 
    if audio_streams:
        first = audio_streams[0]
        return [first["index"]], f"no audio matching {sorted(wanted)} found — kept first audio track as fallback"
 
    return [], "no audio streams found"
 
 
def _compute_target_size(width: int, height: int) -> Optional[tuple]:
    """
    Return (new_width, new_height) if downscaling is needed to fit within
    Full HD (1920x1080) bounding box, else None (no scaling needed).
    Dimensions are rounded down to even numbers (required by most encoders).
    """
    if not width or not height:
        return None

    if width <= 1920 and height <= 1080:
        return None

    scale_factor = min(1920 / width, 1080 / height)
    new_w = int(width * scale_factor) // 2 * 2
    new_h = int(height * scale_factor) // 2 * 2
    return (max(new_w, 2), max(new_h, 2))
 
 
class Converter:
    """Manages video-to-TS conversion for a folder."""
 
    def __init__(self):
        self.files: Dict[str, FileInfo] = {}
        self.source_folder: str = ""
        self._active_processes: Dict[str, subprocess.Popen] = {}
        self._queue: List[str] = []
        self._queue_worker_task: Optional[asyncio.Task] = None
        # Every directory we've ever seen a thumbnail written into, so we can
        # still find + remove orphans even if every file in a folder vanished
        # in one go (in which case this scan has zero "valid" thumbnails to
        # infer the directory from).
        self._known_thumbnail_dirs: set = set()
 
    def scan_configured_folder(self) -> List[dict]:
        """Scan the folder configured in config.yml (converter.source_folder)."""
        cfg = load_config()
        return self.scan_folder(cfg.converter.source_folder)
 
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
                if f.name in old_files:
                    info = old_files[f.name]
                    info.size = f.stat().st_size
                else:
                    info = FileInfo(
                        filename=f.name,
                        filepath=str(f),
                        size=f.stat().st_size,
                        extension=ext,
                        status=ConversionStatus.DONE,
                        progress=1.0,
                        ts_filename=f.name,
                    )
                # Generate thumbnail for .ts files too if missing
                if not info.thumbnail:
                    thumb = generate_thumbnail(str(f))
                    if thumb:
                        info.thumbnail = thumb
                # Fetch metadata if missing
                if not info.metadata or not info.metadata.get("duration"):
                    info.metadata = get_video_metadata(str(f))
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
 
                if f.name in old_files:
                    info = old_files[f.name]
                    info.status = status
                    info.progress = progress
                    info.size = f.stat().st_size
                else:
                    info = FileInfo(
                        filename=f.name,
                        filepath=str(f),
                        size=f.stat().st_size,
                        extension=ext,
                        status=status,
                        progress=progress,
                        ts_filename=ts_name,
                    )
 
                # Generate thumbnail if missing
                if not info.thumbnail:
                    probe_target = str(ts_path if status == ConversionStatus.DONE else f)
                    thumb = generate_thumbnail(probe_target)
                    if thumb:
                        info.thumbnail = thumb
 
                # Get metadata if missing
                if not info.metadata or not info.metadata.get("duration"):
                    probe_target = str(ts_path if status == ConversionStatus.DONE else f)
                    info.metadata = get_video_metadata(probe_target)
 
                self.files[f.name] = info
                results.append(self._file_to_dict(info))
 
        removed = self._cleanup_orphaned_thumbnails()
        if removed:
            logger.info(f"Removed {removed} orphaned thumbnail(s) for missing files.")
 
        return results
 
    def _cleanup_orphaned_thumbnails(self) -> int:
        """
        Remove thumbnail files left behind for videos/.ts files that no
        longer exist in the scanned folder (deleted, renamed, or moved).
        Returns the number of thumbnail files removed.
        """
        valid_thumbs = set()
        for info in self.files.values():
            if info.thumbnail:
                try:
                    valid_thumbs.add(str(Path(info.thumbnail).resolve()))
                    self._known_thumbnail_dirs.add(Path(info.thumbnail).resolve().parent)
                except OSError:
                    continue
 
        removed = 0
        for thumb_dir in list(self._known_thumbnail_dirs):
            if not thumb_dir.exists():
                self._known_thumbnail_dirs.discard(thumb_dir)
                continue
            for f in thumb_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    if str(f.resolve()) not in valid_thumbs:
                        f.unlink()
                        removed += 1
                        logger.info(f"Removed orphaned thumbnail: {f}")
                except OSError as e:
                    logger.warning(f"Could not remove orphaned thumbnail {f}: {e}")
 
        return removed
 
    def cleanup_orphaned_thumbnails(self) -> int:
        """Public wrapper so callers (e.g. an API route) can trigger cleanup on demand."""
        return self._cleanup_orphaned_thumbnails()
 
    async def convert_file(self, filename: str) -> bool:
        """Add a file to the conversion queue."""
        if not is_ffmpeg_installed():
            logger.error("FFmpeg not installed")
            return False

        if filename not in self.files:
            logger.error(f"File not found: {filename}")
            return False

        info = self.files[filename]
        if info.status in (ConversionStatus.DONE, ConversionStatus.CONVERTING):
            return True

        if filename not in self._queue:
            self._queue.append(filename)
            logger.info(f"Added {filename} to conversion queue. Queue length: {len(self._queue)}")

        self._start_queue_worker()
        return True

    async def convert_all(self) -> int:
        """Add all pending files to the conversion queue. Returns count of added files."""
        count = 0
        for filename, info in self.files.items():
            if info.status == ConversionStatus.PENDING:
                if filename not in self._queue:
                    self._queue.append(filename)
                    count += 1
        if count > 0:
            logger.info(f"Added {count} files to conversion queue. Queue length: {len(self._queue)}")
            self._start_queue_worker()
        return count

    async def stop_conversion(self) -> bool:
        """Stop all active conversions, clear the queue, and delete any incomplete output files."""
        logger.info("Stopping all conversions...")

        # 1. Clear the queue
        self._queue.clear()

        # 2. Cancel the queue worker task
        if self._queue_worker_task and not self._queue_worker_task.done():
            self._queue_worker_task.cancel()
            self._queue_worker_task = None

        # 3. Terminate all active processes
        active_filenames = list(self._active_processes.keys())
        for filename in active_filenames:
            process = self._active_processes.get(filename)
            if process:
                try:
                    process.terminate()
                except Exception as e:
                    logger.warning(f"Error terminating process for {filename}: {e}")

        # Let the OS clean up processes
        await asyncio.sleep(0.5)

        # 4. Clean up files and reset statuses
        for fname, info in self.files.items():
            if info.status in (ConversionStatus.CONVERTING, ConversionStatus.QUEUED):
                info.status = ConversionStatus.PENDING
                info.progress = 0.0
                info.error = ""
                # Delete the incomplete .ts file
                try:
                    output_path = Path(info.filepath).parent / info.ts_filename
                    if output_path.exists():
                        output_path.unlink(missing_ok=True)
                        logger.info(f"Deleted incomplete file: {output_path}")
                except Exception as e:
                    logger.warning(f"Could not delete incomplete file for {fname}: {e}")

        # Rescan the directory to ensure state is clean
        self.scan_folder(self.source_folder)
        return True

    def _start_queue_worker(self):
        if self._queue_worker_task is None or self._queue_worker_task.done():
            self._queue_worker_task = asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        logger.info("Starting queue worker loop")
        while self._queue:
            filename = self._queue[0]
            info = self.files.get(filename)
            if not info:
                self._queue.pop(0)
                continue

            if info.status == ConversionStatus.DONE:
                self._queue.pop(0)
                continue

            info.status = ConversionStatus.CONVERTING
            info.progress = 0.0
            info.error = ""

            try:
                await self._run_conversion(filename)
            except Exception as e:
                logger.error(f"Error running conversion for {filename}: {e}")
                info.status = ConversionStatus.ERROR
                info.error = str(e)
            finally:
                if self._queue and self._queue[0] == filename:
                    self._queue.pop(0)

        logger.info("Queue worker loop finished (queue empty)")
 
    async def _run_conversion(self, filename: str):
        """Run the actual FFmpeg conversion process."""
        info = self.files[filename]
        input_path = info.filepath
        output_path = str(Path(input_path).parent / info.ts_filename)
 
        try:
            # Probe the file so we know which audio track(s) to keep and
            # whether the video needs to be scaled down to HD.
            streams = await probe_streams(input_path)
 
            cfg = load_config()
            languages = cfg.converter.languages or ["tur", "tr", "trk"]
 
            audio_indices, audio_note = _select_audio_by_language(streams["audio"], languages)
            info.audio_note = audio_note
            logger.info(f"{filename}: {audio_note}")
 
            target_size = None
            if streams["video"]:
                target_size = _compute_target_size(streams["video"]["width"], streams["video"]["height"])
 
            needs_scale = target_size is not None
            info.scaled_note = (
                f"downscaling to {target_size[0]}x{target_size[1]} (HD cap)"
                if needs_scale else "no scaling needed (already HD or smaller)"
            )
            logger.info(f"{filename}: {info.scaled_note}")
 
            video_stream_index = streams["video"]["index"] if streams["video"] else None
            success = False
 
            # Try hardware encoding if supported
            from app.ffmpeg_setup import get_best_encoder
            best_encoder = get_best_encoder()
            if best_encoder in ("h264_nvenc", "h264_qsv"):
                logger.info(f"Re-encoding with hardware ({best_encoder}) for {filename}...")
                success = await self._ffmpeg_convert(
                    input_path, output_path, filename, strategy="hardware",
                    video_stream_index=video_stream_index,
                    audio_indices=audio_indices,
                    target_size=target_size,
                )
 
            # CPU fallback
            if not success:
                logger.info(f"Re-encoding with CPU (libx264) for {filename}...")
                info.progress = 0.0
                success = await self._ffmpeg_convert(
                    input_path, output_path, filename, strategy="cpu",
                    video_stream_index=video_stream_index,
                    audio_indices=audio_indices,
                    target_size=target_size,
                )
 
            if success:
                info.status = ConversionStatus.DONE
                info.progress = 1.0
                # Move original file to 'original' subfolder
                try:
                    original_folder = Path(input_path).parent / "original"
                    original_folder.mkdir(exist_ok=True)
                    original_path = original_folder / Path(input_path).name
                    shutil.move(str(input_path), str(original_path))
                    info.filepath = str(original_path)
                except Exception as e:
                    logger.warning(f"Could not move original file: {e}")
 
                # Generate thumbnail and update metadata for the new .ts file
                thumb = generate_thumbnail(output_path)
                if thumb:
                    info.thumbnail = thumb
                info.metadata = get_video_metadata(output_path)
                if Path(output_path).exists():
                    info.size = Path(output_path).stat().st_size
 
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
            self._active_processes.pop(filename, None)
            self.scan_folder(self.source_folder)
 
    async def _ffmpeg_convert(
        self,
        input_path: str,
        output_path: str,
        filename: str,
        strategy: str,
        video_stream_index: Optional[int],
        audio_indices: List[int],
        target_size: Optional[tuple],
    ) -> bool:
        """Run FFmpeg conversion and track progress."""
        info = self.files[filename]
 
        # Get duration for progress calculation
        duration = info.metadata.get("duration", 0)
 
        base_cmd = [get_ffmpeg_path(), "-y", "-hwaccel", "auto", "-i", input_path]
 
        # --- Stream mapping: explicit video + only the selected audio tracks.
        # Subtitles are dropped simply by never mapping them.
        map_args = []
        if video_stream_index is not None:
            map_args += ["-map", f"0:{video_stream_index}"]
        else:
            map_args += ["-map", "0:v:0"]  # best-effort fallback
        for a_idx in audio_indices:
            map_args += ["-map", f"0:{a_idx}"]
 
        scale_args = []
        if target_size:
            scale_args = ["-vf", f"scale={target_size[0]}:{target_size[1]}"]
 
        if strategy == "hardware":
            from app.ffmpeg_setup import get_best_encoder, get_encoding_params
            best_encoder = get_best_encoder()
            hw_args = get_encoding_params(best_encoder)
            cmd = base_cmd + map_args + scale_args + hw_args + [
                "-c:a", "aac",
                "-b:a", "256k",            # High quality stereo audio
                "-ac", "2",                # Force stereo
                "-bsf:v", "dump_extra",    # Repeat SPS/PPS headers before keyframes
                "-f", "mpegts",
                output_path,
            ]
        else:
            # CPU Fallback (libx264)
            from app.ffmpeg_setup import get_encoding_params
            cpu_args = get_encoding_params("libx264")
            cmd = base_cmd + map_args + scale_args + cpu_args + [
                "-c:a", "aac",
                "-b:a", "256k",
                "-ac", "2",
                "-bsf:v", "dump_extra",    # Repeat SPS/PPS headers before keyframes
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
            "has_thumbnail": info.thumbnail is not None or Path(info.filepath).exists(),
            "audio_note": info.audio_note,
            "scaled_note": info.scaled_note,
        }
 
 
# Singleton converter instance
converter = Converter()