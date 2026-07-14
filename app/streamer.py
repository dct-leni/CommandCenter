"""
RTMP Streaming scheduler and process manager.
Manages MediaMTX instances and FFmpeg push processes for date-range folders.
"""

import asyncio
import os
import re
import subprocess
import logging
import tempfile
import time
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.ffmpeg_setup import get_ffmpeg_path, get_mediamtx_path, is_ffmpeg_installed, is_mediamtx_installed
from app.thumbnails import generate_thumbnail, get_video_metadata
from app.config import load_config, update_config

logger = logging.getLogger(__name__)


@dataclass
class StreamInfo:
    """Info about a single active stream (one .ts file on one port)."""
    filename: str
    filepath: str
    port: int
    rtmp_url: str  # Internal ingest URL
    stream_url: str = ""  # Public playback URL for UI
    status: str = "starting"  # starting, live, error, stopped
    error: str = ""
    progress: float = 0.0
    start_offset: float = 0.0  # Time skipped at start for sync
    metadata: dict = field(default_factory=dict)
    log_task: Optional[asyncio.Task] = field(default=None, repr=False)
    mediamtx_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    ffmpeg_process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)


@dataclass
class DateRangeFolder:
    """Parsed date-range folder info."""
    name: str
    path: str
    start_date: date
    end_date: date
    files: List[str] = field(default_factory=list)
    is_active: bool = False


class Streamer:
    """Manages RTMP streaming of .ts files from date-range folders."""

    def __init__(self):
        self.content_folder: str = ""
        self.folders: List[DateRangeFolder] = []
        self.active_streams: Dict[int, StreamInfo] = {}  # port -> StreamInfo
        self.is_running: bool = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self._port_range_start: int = 1935
        self._port_range_end: int = 1944
        self._current_folder_name: str = ""
        self._errors: List[str] = []

    def scan_content_folder(self, folder_path: str) -> List[dict]:
        """Scan root folder for date-range subfolders (DDMM_DDMM format)."""
        self.content_folder = folder_path
        root = Path(folder_path)

        if not root.exists() or not root.is_dir():
            return []

        self.folders.clear()
        results = []
        current_year = datetime.now().year

        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue

            parsed = self._parse_folder_name(d.name, current_year)
            if parsed is None:
                continue

            start_date, end_date = parsed

            # Collect .ts files in the folder
            ts_files = sorted([
                f.name for f in d.iterdir()
                if f.is_file() and f.suffix.lower() == ".ts"
            ])

            folder_info = DateRangeFolder(
                name=d.name,
                path=str(d),
                start_date=start_date,
                end_date=end_date,
                files=ts_files,
                is_active=(start_date <= date.today() <= end_date),
            )

            self.folders.append(folder_info)
            results.append(self._folder_to_dict(folder_info))

        return results

    def get_folder_details(self, folder_name: str) -> Optional[dict]:
        """Get detailed info about a specific date-range folder, including file metadata."""
        folder = self._find_folder(folder_name)
        if not folder:
            return None

        folder_path = Path(folder.path)
        files_detail = []

        for fname in folder.files:
            fpath = folder_path / fname
            if not fpath.exists():
                continue

            meta = get_video_metadata(str(fpath))
            thumb = generate_thumbnail(str(fpath))

            # Check if this file is currently being streamed
            stream_port = None
            stream_status = None
            for port, stream in self.active_streams.items():
                if stream.filename == fname:
                    stream_port = port
                    stream_status = stream.status
                    break

            files_detail.append({
                "filename": fname,
                "size": fpath.stat().st_size,
                "metadata": meta,
                "has_thumbnail": thumb is not None,
                "stream_port": stream_port,
                "stream_status": stream_status,
            })

        result = self._folder_to_dict(folder)
        result["files_detail"] = files_detail
        return result

    async def start_streaming(self, port_range_start: int, port_range_end: int) -> dict:
        """Start the streaming scheduler."""
        if self.is_running:
            return {"status": "already_running"}

        if not is_ffmpeg_installed():
            return {"status": "error", "error": "FFmpeg not found. Run setup_binaries.bat first."}
        if not is_mediamtx_installed():
            return {"status": "error", "error": "MediaMTX not found. Run setup_binaries.bat first."}
        if not self.content_folder:
            return {"status": "error", "error": "No content folder selected."}

        self._port_range_start = port_range_start
        self._port_range_end = port_range_end
        self._errors.clear()
        self.is_running = True

        # Rescan folders to get fresh data
        self.scan_content_folder(self.content_folder)

        # Start scheduler loop
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

        return {"status": "started"}

    async def stop_streaming(self, is_shutdown: bool = False) -> dict:
        """Stop all active streams and the scheduler."""
        self.is_running = False

        if not is_shutdown:
            update_config({"streamer": {"auto_resume": False}})

        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

        await self._stop_all_streams()
        self._current_folder_name = ""

        return {"status": "stopped"}

    def get_status(self) -> dict:
        """Get current streaming status."""
        streams = []
        for port, info in self.active_streams.items():
            streams.append({
                "filename": info.filename,
                "port": info.port,
                "rtmp_url": info.rtmp_url,
                "stream_url": info.stream_url,  # New: Display URL for Web UI
                "status": info.status,
                "error": info.error,
                "progress": round(info.progress, 3),
                "metadata": info.metadata,
            })

        return {
            "is_running": self.is_running,
            "current_folder": self._current_folder_name,
            "active_streams": streams,
            "port_range": f"{self._port_range_start}-{self._port_range_end}",
            "errors": self._errors[-10:],  # Last 10 errors
        }

    # ---- Internal methods ----

    def _find_folder(self, name: str) -> Optional[DateRangeFolder]:
        """Find a parsed folder by name."""
        for f in self.folders:
            if f.name == name:
                return f
        return None
        
    async def _scheduler_loop(self):
        """Main scheduler loop — checks date and manages folder transitions."""
        try:
            while self.is_running:
                today = date.today()
                active_folder = self._get_active_folder(today)

                if active_folder is None:
                    # No folder matches today's date
                    if self.active_streams:
                        logger.info("No active folder for today, stopping streams.")
                        await self._stop_all_streams()
                        self._current_folder_name = ""
                    await asyncio.sleep(60)
                    continue

                # Check if we need to switch folders
                if active_folder.name != self._current_folder_name:
                    update_config({"streamer": {
                        "current_folder": active_folder.name
                    }})
                    logger.info(f"Loading folder: {active_folder.name} (Syncing to 00:00)")

                    # Stop current streams (let them finish gracefully)
                    await self._stop_all_streams()

                    # Start streams for new folder
                    await self._start_folder_streams(active_folder)
                    self._current_folder_name = active_folder.name

                # Check for dead streams and restart them
                await self._health_check()

                await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled.")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            self._errors.append(f"Scheduler error: {e}")
            self.is_running = False

    async def _start_folder_streams(self, folder: DateRangeFolder):
        """Start streaming all .ts files in a folder, one per RTMP port."""
        available_ports = list(range(self._port_range_start, self._port_range_end + 1))
        ts_files = folder.files

        if len(ts_files) > len(available_ports):
            error_msg = (
                f"Folder '{folder.name}' has {len(ts_files)} files but only "
                f"{len(available_ports)} ports available ({self._port_range_start}-{self._port_range_end}). "
                f"Increase port range in config."
            )
            logger.error(error_msg)
            self._errors.append(error_msg)
            # Stream as many as we can
            ts_files = ts_files[:len(available_ports)]

        for i, filename in enumerate(ts_files):
            port = available_ports[i]
            filepath = str(Path(folder.path) / filename)

            if not Path(filepath).exists():
                logger.warning(f"File not found: {filepath}")
                continue

            await self._start_single_stream(filename, filepath, port)

    async def _start_single_stream(self, filename: str, filepath: str, port: int):
        """Start MediaMTX + FFmpeg for a single file on a specific port."""
        cfg = load_config()
        protocol = cfg.streamer.protocol.lower()

        # The 'port' parameter is the public port requested by the user in the UI.
        # If the user wants HLS on the public port, we must move the internal RTMP 
        # ingest port to a hidden one to prevent a binding conflict in MediaMTX.
        if protocol == "hls":
            public_port = port
            internal_rtmp_port = port + 6000
            stream_url = f"http://127.0.0.1:{public_port}/live/stream/index.m3u8"
        else:
            public_port = port
            internal_rtmp_port = port
            stream_url = f"rtmp://127.0.0.1:{public_port}/live/stream"

        ingest_url = f"rtmp://127.0.0.1:{internal_rtmp_port}/live/stream"

        metadata = get_video_metadata(filepath)
        duration = metadata.get("duration", 0)
        
        # Calculate time since 00:00 (midnight) today
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_since_midnight = (now - midnight).total_seconds()

        offset = 0.0
        if duration > 0:
            offset = elapsed_since_midnight % duration

        stream_info = StreamInfo(
            filename=filename,
            filepath=filepath,
            port=port,
            rtmp_url=ingest_url,
            stream_url=stream_url,
            status="starting",
            start_offset=offset,
            metadata=metadata,
        )
        self.active_streams[port] = stream_info

        try:
            # 1. Create a minimal MediaMTX config for this port
            mtx_config = self._create_mediamtx_config(internal_rtmp_port, public_port, protocol)

            # 2. Start MediaMTX
            mtx_process = subprocess.Popen(
                [get_mediamtx_path(), mtx_config],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stream_info.mediamtx_process = mtx_process

            # Give MediaMTX time to start
            await asyncio.sleep(2)

            if mtx_process.poll() is not None:
                stderr = mtx_process.stderr.read().decode("utf-8", errors="replace") if mtx_process.stderr else ""
                stream_info.status = "error"
                stream_info.error = f"MediaMTX failed to start on port {port}: {stderr[-300:]}"
                logger.error(stream_info.error)
                self._errors.append(stream_info.error)
                return

            # 3. Start FFmpeg pushing to MediaMTX
            ffmpeg_cmd = [
                get_ffmpeg_path(),
                "-re",                    # Read at native frame rate
                "-stream_loop", "-1",     # Loop forever
            ]
            
            # Jump directly to sync position before reading input (fast seek)
            if offset > 0:
                ffmpeg_cmd.extend(["-ss", str(offset)])
                
            ffmpeg_cmd.extend([
                "-i", filepath,
                "-c", "copy",             # No re-encoding
                "-f", "flv",              # RTMP output format
                ingest_url,
            ])

            ffmpeg_process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stream_info.ffmpeg_process = ffmpeg_process

            # Give FFmpeg a moment to connect
            await asyncio.sleep(1)

            if ffmpeg_process.returncode is not None:
                stderr = (await ffmpeg_process.stderr.read()).decode("utf-8", errors="replace")
                stream_info.status = "error"
                stream_info.error = f"FFmpeg failed to start: {stderr[-300:]}"
                logger.error(stream_info.error)
                self._errors.append(stream_info.error)
                return

            stream_info.status = "live"
            logger.info(f"Stream started: {filename} on public port {public_port} (Protocol: {protocol.upper()})")
            
            # Start background task to read logs and update progress
            stream_info.log_task = asyncio.create_task(
                self._read_ffmpeg_logs(stream_info, ffmpeg_process.stderr)
            )

        except Exception as e:
            stream_info.status = "error"
            stream_info.error = str(e)
            logger.error(f"Failed to start stream for {filename}: {e}")
            self._errors.append(str(e))

    def _create_mediamtx_config(self, internal_rtmp_port: int, public_port: int, protocol: str) -> str:
        """Create a temporary MediaMTX YAML config for a specific RTMP port and target protocol."""
        
        hls_block = "hls: no"
        if protocol == "hls":
            hls_block = f"hls: yes\nhlsAddress: :{public_port}\nhlsAlwaysRemux: yes\nhlsVariant: lowLatency"

        # Disable all unused protocols, assign unique ports to avoid conflicts
        config_content = f"""
logLevel: warn
logDestinations: [stdout]

# RTMP Ingest (Always used by FFmpeg)
rtmpAddress: :{internal_rtmp_port}

# Target Protocol configuration
{hls_block}

# Disable other protocols to avoid port conflicts
rtsp: no
webrtc: no
srt: no
api: no
moq: no

paths:
  all:
    source: publisher
"""
        config_dir = Path(tempfile.gettempdir()) / "commandcenter"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"mediamtx_{public_port}.yml"

        with open(config_path, "w") as f:
            f.write(config_content)

        return str(config_path)

    async def _stop_all_streams(self):
        """Stop all active MediaMTX and FFmpeg processes."""
        for port, stream in list(self.active_streams.items()):
            await self._stop_single_stream(port)
        self.active_streams.clear()

    async def _stop_single_stream(self, port: int):
        """Stop a single stream (FFmpeg + MediaMTX)."""
        stream = self.active_streams.get(port)
        if not stream:
            return

        # Cancel log task
        if stream.log_task:
            stream.log_task.cancel()

        # Stop FFmpeg first
        if stream.ffmpeg_process and stream.ffmpeg_process.returncode is None:
            try:
                stream.ffmpeg_process.terminate()
                try:
                    await asyncio.wait_for(stream.ffmpeg_process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    stream.ffmpeg_process.kill()
            except Exception as e:
                logger.warning(f"Error stopping FFmpeg on port {port}: {e}")

        # Stop MediaMTX
        if stream.mediamtx_process and stream.mediamtx_process.poll() is None:
            try:
                stream.mediamtx_process.terminate()
                stream.mediamtx_process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"Error stopping MediaMTX on port {port}: {e}")
                try:
                    stream.mediamtx_process.kill()
                except Exception:
                    pass

        stream.status = "stopped"
        logger.info(f"Stream stopped on port {port}")

    async def _health_check(self):
        """Check for dead streams and restart them."""
        for port, stream in list(self.active_streams.items()):
            if stream.status != "live":
                continue

            # Check if FFmpeg is still running
            if stream.ffmpeg_process and stream.ffmpeg_process.returncode is not None:
                logger.warning(f"FFmpeg died on port {port}, restarting with synced offset...")
                await self._stop_single_stream(port)
                await self._start_single_stream(stream.filename, stream.filepath, port)

            # Check if MediaMTX is still running
            elif stream.mediamtx_process and stream.mediamtx_process.poll() is not None:
                logger.warning(f"MediaMTX died on port {port}, restarting with synced offset...")
                await self._stop_single_stream(port)
                await self._start_single_stream(stream.filename, stream.filepath, port)

    async def _read_ffmpeg_logs(self, stream_info: StreamInfo, stderr_stream):
        """Read FFmpeg stderr chunk by chunk to calculate streaming progress."""
        duration = stream_info.metadata.get("duration", 0)
        buffer = ""
        try:
            while True:
                chunk = await stderr_stream.read(4096)
                if not chunk:
                    break
                
                buffer += chunk.decode("utf-8", errors="replace")
                
                if '\r' in buffer or '\n' in buffer:
                    lines = buffer.replace('\r', '\n').split('\n')
                    buffer = lines.pop()  # Keep the last incomplete part in the buffer
                    
                    for line in lines:
                        # Extract time from FFmpeg output
                        match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                        if match and duration > 0:
                            current_time = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                            
                            # Because FFmpeg's time= starts at 0 due to the -ss flag,
                            # we must add the start offset to accurately represent UI progress 
                            # inside the full video loop
                            total_time = current_time + stream_info.start_offset
                            stream_info.progress = (total_time % duration) / duration
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading ffmpeg logs for {stream_info.filename}: {e}")

    def _get_active_folder(self, today: date) -> Optional[DateRangeFolder]:
        """Find the folder whose date range includes today. If overlap, nearest start_date wins."""
        candidates = [f for f in self.folders if f.start_date <= today <= f.end_date]

        if not candidates:
            return None

        # Sort by start_date descending (closest/most recent start wins)
        candidates.sort(key=lambda f: f.start_date, reverse=True)
        return candidates[0]

    @staticmethod
    def _parse_folder_name(name: str, year: int) -> Optional[Tuple[date, date]]:
        """
        Parse folder name in DDMM_DDMM format.
        Example: '0103_0503' → (date(2026, 3, 1), date(2026, 3, 5))
        """
        match = re.match(r"^(\d{2})(\d{2})_(\d{2})(\d{2})$", name)
        if not match:
            return None

        try:
            start_day, start_month = int(match.group(1)), int(match.group(2))
            end_day, end_month = int(match.group(3)), int(match.group(4))

            start = date(year, start_month, start_day)
            end = date(year, end_month, end_day)

            # Handle year-wrapping (e.g., 2512_0501 = Dec 25 → Jan 5)
            if end < start:
                end = date(year + 1, end_month, end_day)

            return (start, end)
        except ValueError:
            return None

    def _folder_to_dict(self, folder: DateRangeFolder) -> dict:
        """Convert DateRangeFolder to JSON-serializable dict."""
        return {
            "name": folder.name,
            "path": folder.path,
            "start_date": folder.start_date.isoformat(),
            "end_date": folder.end_date.isoformat(),
            "display_range": f"{folder.start_date.strftime('%d.%m')} → {folder.end_date.strftime('%d.%m')}",
            "file_count": len(folder.files),
            "files": folder.files,
            "is_active": folder.start_date <= date.today() <= folder.end_date,
        }


# Singleton streamer instance
streamer = Streamer()