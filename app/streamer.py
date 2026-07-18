"""
RTMP Streaming scheduler and process manager.
Manages MediaMTX instances and FFmpeg push processes for date-range folders.
"""

import asyncio
import os
import re
import shutil
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
from app.config import load_config, save_config, update_config

logger = logging.getLogger(__name__)


@dataclass
class StreamInfo:
    """Info about a single active stream (one port, potentially multiple files)."""
    port: int
    slot_files: List[str]      # ordered list of filenames in this slot
    slot_paths: List[str]      # absolute paths matching slot_files
    slot_durations: List[float]  # duration of each file in seconds
    rtmp_url: str              # Internal ingest URL
    stream_url: str = ""       # Public playback URL for UI
    status: str = "starting"   # starting, live, error, stopped
    error: str = ""
    progress: float = 0.0
    current_file_index: int = 0  # which file in slot is currently playing
    start_offset: float = 0.0   # Time skipped at start for sync
    cycle_start_offset: float = 0.0 # Time offset since beginning of the cycle
    metadata: dict = field(default_factory=dict)  # metadata of first file in slot
    log_task: Optional[asyncio.Task] = field(default=None, repr=False)
    mediamtx_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    ffmpeg_process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)

    @property
    def filename(self) -> str:
        """Current active file name (for compatibility)."""
        if self.current_file_index < len(self.slot_files):
            return self.slot_files[self.current_file_index]
        return self.slot_files[0] if self.slot_files else ""

    @property
    def filepath(self) -> str:
        """Current active file path (for compatibility)."""
        if self.current_file_index < len(self.slot_paths):
            return self.slot_paths[self.current_file_index]
        return self.slot_paths[0] if self.slot_paths else ""

    def to_dict(self) -> dict:
        """Return a clean, JSON-serializable dict without Task/Process objects."""
        return {
            "port": self.port,
            "filename": self.filename,
            "filepath": self.filepath,
            "slot_files": self.slot_files,
            "slot_paths": self.slot_paths,
            "slot_durations": self.slot_durations,
            "rtmp_url": self.rtmp_url,
            "stream_url": self.stream_url,
            "status": self.status,
            "error": self.error,
            "progress": round(self.progress, 3),
            "current_file_index": self.current_file_index,
            "start_offset": self.start_offset,
            "cycle_start_offset": self.cycle_start_offset,
            "metadata": self.metadata,
        }


@dataclass
class PortSlot:
    """A port slot: one RTMP port with an ordered list of files to play round-robin."""
    port: int
    files: List[str] = field(default_factory=list)    # filenames only
    paths: List[str] = field(default_factory=list)    # absolute paths
    durations: List[float] = field(default_factory=list)  # in seconds


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

    @property
    def port_range_start(self) -> int:
        cfg = load_config()
        return cfg.streamer.port_range_start if cfg.streamer.port_range_start else self._port_range_start

    @property
    def port_range_end(self) -> int:
        cfg = load_config()
        return cfg.streamer.port_range_end if cfg.streamer.port_range_end else self._port_range_end

    def cleanup_playlists(self) -> None:
        """Remove missing folders and out-of-range ports from streamer playlists in config.yml, returning unused files to converter."""
        cfg = load_config()
        playlists = dict(cfg.streamer.playlists)
        changed = False

        content_dir = Path(self.content_folder or cfg.streamer.content_folder)
        valid_folders = set()
        if content_dir.is_dir():
            valid_folders = set(d.name for d in content_dir.iterdir() if d.is_dir())
            for folder_name in list(playlists.keys()):
                if folder_name not in valid_folders:
                    del playlists[folder_name]
                    changed = True

        available_ports = set(range(self.port_range_start, self.port_range_end + 1))
        from app.converter import converter

        for folder_name in list(playlists.keys()):
            slots = playlists[folder_name]
            if not isinstance(slots, list):
                del playlists[folder_name]
                changed = True
                continue

            cleaned_slots = []
            removed_files = []
            for s in slots:
                if not isinstance(s, dict):
                    changed = True
                    continue
                port = s.get("port")
                if port is None or port not in available_ports:
                    changed = True
                    for fname in s.get("files", []):
                        removed_files.append(fname)
                    continue
                cleaned_slots.append(s)

            cleaned_slots.sort(key=lambda x: x.get("port", 0))

            if cleaned_slots != slots:
                changed = True

            for fname in removed_files:
                still_used = any(fname in cs.get("files", []) for cs in cleaned_slots)
                if not still_used and self.content_folder:
                    source_file = Path(self.content_folder) / folder_name / fname
                    if source_file.exists() and converter.source_folder:
                        target_file = Path(converter.source_folder) / fname
                        try:
                            import shutil
                            shutil.move(str(source_file), str(target_file))
                            logger.info(f"Returned {fname} from out-of-range slot in {folder_name} to converter input folder")
                            if converter.source_folder:
                                converter.scan_folder(converter.source_folder)
                        except Exception as e:
                            logger.error(f"Failed to return {fname} to converter folder: {e}")

            if not cleaned_slots:
                del playlists[folder_name]
                changed = True
            else:
                playlists[folder_name] = cleaned_slots

        if changed:
            update_config({"streamer": {"playlists": playlists}})
            if self.content_folder:
                self.scan_content_folder(self.content_folder)

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

        self.cleanup_playlists()
        return results

    def get_folder_details(self, folder_name: str) -> Optional[dict]:
        """Get detailed info about a specific date-range folder, including slot/file metadata."""
        self.cleanup_playlists()
        folder = self._find_folder(folder_name)
        if not folder:
            return None

        folder_path = Path(folder.path)
        cfg = load_config()

        # Load slot configuration for this folder
        slots_cfg = cfg.streamer.playlists.get(folder_name, [])

        # Build per-slot detail list
        slots_detail = []
        used_files = set()
        available_ports = list(range(self.port_range_start, self.port_range_end + 1))

        if slots_cfg:
            for slot_entry in slots_cfg:
                port = slot_entry.get("port")
                if port is None or port not in available_ports:
                    continue
                slot_files = slot_entry.get("files", [])
                files_detail = []
                for fname in slot_files:
                    fpath = folder_path / fname
                    if not fpath.exists():
                        # Check if file is in converter source folder
                        from app.converter import converter
                        conv_path = Path(converter.source_folder) / fname if converter.source_folder else None
                        if conv_path and conv_path.exists():
                            meta = get_video_metadata(str(conv_path))
                            files_detail.append({
                                "filename": fname,
                                "size": conv_path.stat().st_size,
                                "metadata": meta,
                                "has_thumbnail": generate_thumbnail(str(conv_path)) is not None,
                            })
                        else:
                            files_detail.append({
                                "filename": fname,
                                "size": 0,
                                "metadata": {},
                                "has_thumbnail": False,
                            })
                        continue
                    used_files.add(fname)
                    meta = get_video_metadata(str(fpath))
                    # Check live stream status
                    stream_info = None
                    if port in self.active_streams:
                        si = self.active_streams[port]
                        stream_info = {
                            "status": si.status,
                            "progress": round(si.progress, 3),
                            "current_file": si.filename,
                        }
                    files_detail.append({
                        "filename": fname,
                        "size": fpath.stat().st_size if fpath.exists() else 0,
                        "metadata": meta,
                        "has_thumbnail": generate_thumbnail(str(fpath)) is not None,
                    })
                slots_detail.append({
                    "port": port,
                    "files": slot_files,
                    "files_detail": files_detail,
                    "stream_info": self.active_streams[port].to_dict() if port in self.active_streams else None,
                    "stream_status": self.active_streams[port].status if port in self.active_streams else None,
                    "stream_progress": round(self.active_streams[port].progress, 3) if port in self.active_streams else 0.0,
                    "stream_current_file": self.active_streams[port].filename if port in self.active_streams else None,
                })
        else:
            # Fallback: auto-assign files to ports in alphabetical order
            for i, fname in enumerate(folder.files):
                if i >= len(available_ports):
                    break
                port = available_ports[i]
                fpath = folder_path / fname
                meta = get_video_metadata(str(fpath)) if fpath.exists() else {}
                files_detail = [{
                    "filename": fname,
                    "size": fpath.stat().st_size if fpath.exists() else 0,
                    "metadata": meta,
                    "has_thumbnail": generate_thumbnail(str(fpath)) is not None,
                }]
                slots_detail.append({
                    "port": port,
                    "files": [fname],
                    "files_detail": files_detail,
                    "stream_info": self.active_streams[port].to_dict() if port in self.active_streams else None,
                    "stream_status": self.active_streams[port].status if port in self.active_streams else None,
                    "stream_progress": round(self.active_streams[port].progress, 3) if port in self.active_streams else 0.0,
                    "stream_current_file": self.active_streams[port].filename if port in self.active_streams else None,
                })

        slots_detail.sort(key=lambda s: s["port"])
        result = self._folder_to_dict(folder)
        result["slots"] = slots_detail
        result["all_files"] = folder.files
        return result

    def create_folder(self, name: str) -> List[dict]:
        """Create a new date-range folder inside content_folder."""
        if not re.match(r"^\d{4}_\d{4}$", name):
            raise ValueError("Invalid folder format. Must be DDMM_DDMM (e.g. 1207_1907)")
        if not self._parse_folder_name(name, datetime.now().year):
            raise ValueError("Invalid date in folder name (DDMM_DDMM)")

        if not self.content_folder:
            cfg = load_config()
            self.content_folder = cfg.streamer.content_folder or "streams"

        root = Path(self.content_folder)
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        if target.exists():
            raise ValueError(f"Folder '{name}' already exists")

        target.mkdir(parents=True, exist_ok=True)
        return self.scan_content_folder(str(root))

    def rename_folder(self, old_name: str, new_name: str) -> List[dict]:
        """Rename/modify date range of an existing folder."""
        folder = self._find_folder(old_name)
        if not folder:
            raise ValueError(f"Folder '{old_name}' not found")

        if self.is_running and (old_name == self._current_folder_name or folder.is_active):
            raise ValueError("Cannot modify the active folder while streaming is running. Stop streaming first.")

        if not re.match(r"^\d{4}_\d{4}$", new_name):
            raise ValueError("Invalid folder format. Must be DDMM_DDMM (e.g. 1207_1907)")
        if not self._parse_folder_name(new_name, datetime.now().year):
            raise ValueError("Invalid date in new folder name (DDMM_DDMM)")

        if old_name == new_name:
            return self.scan_content_folder(self.content_folder)

        root = Path(self.content_folder or "streams")
        target = root / new_name
        if target.exists():
            raise ValueError(f"Folder '{new_name}' already exists")

        Path(folder.path).rename(target)

        cfg = load_config()
        changed = False
        if old_name in cfg.streamer.playlists:
            cfg.streamer.playlists[new_name] = cfg.streamer.playlists.pop(old_name)
            changed = True
        if cfg.streamer.current_folder == old_name:
            cfg.streamer.current_folder = new_name
            changed = True
        if changed:
            save_config(cfg)

        if self._current_folder_name == old_name:
            self._current_folder_name = new_name

        return self.scan_content_folder(str(root))

    def delete_folder(self, folder_name: str) -> List[dict]:
        """Delete a non-active folder, moving any .ts files inside it back to the converter input folder."""
        folder = self._find_folder(folder_name)
        if not folder:
            raise ValueError(f"Folder '{folder_name}' not found")

        # Must not be active or streaming
        if folder.is_active or (self.is_running and folder_name == self._current_folder_name):
            raise ValueError("Cannot remove the active folder.")

        cfg = load_config()
        source_folder = cfg.converter.source_folder or "input"
        dest_dir = Path(source_folder)
        dest_dir.mkdir(parents=True, exist_ok=True)

        folder_path = Path(folder.path)
        if folder_path.exists():
            for item in folder_path.iterdir():
                if item.is_file():
                    if item.suffix.lower() == ".ts":
                        # Move to converter input
                        shutil.move(str(item), str(dest_dir / item.name))
                    else:
                        # Delete other files (e.g. thumbnails, custom files)
                        item.unlink(missing_ok=True)
            # Remove directory
            try:
                shutil.rmtree(str(folder_path))
            except Exception as e:
                # If rmtree fails, try plain rmdir
                logger.warning(f"rmtree failed for {folder_path}, trying rmdir: {e}")
                folder_path.rmdir()

        # Update config playlists if folder was there
        changed = False
        if folder_name in cfg.streamer.playlists:
            cfg.streamer.playlists.pop(folder_name)
            changed = True
        if cfg.streamer.current_folder == folder_name:
            cfg.streamer.current_folder = ""
            changed = True
        if changed:
            save_config(cfg)

        if self._current_folder_name == folder_name:
            self._current_folder_name = ""

        return self.scan_content_folder(self.content_folder)

    def update_slot(self, folder_name: str, port: int, files: List[str]) -> bool:
        """Update the file list for a specific port slot in a folder and move files if needed."""
        cfg = load_config()
        playlists = dict(cfg.streamer.playlists)
        folder_slots = list(playlists.get(folder_name, []))

        # Physically move files from converter input folder if they aren't in this folder yet
        if self.content_folder:
            from app.converter import converter
            target_dir = Path(self.content_folder) / folder_name
            if target_dir.exists():
                for fname in files:
                    target_file = target_dir / fname
                    if not target_file.exists() and converter.source_folder:
                        source_file = Path(converter.source_folder) / fname
                        if source_file.exists():
                            try:
                                import shutil
                                shutil.move(str(source_file), str(target_file))
                                logger.info(f"Moved {fname} from converter source to {folder_name}")
                                converter.scan_folder(converter.source_folder)
                            except Exception as e:
                                logger.error(f"Failed to move {fname} into {folder_name}: {e}")

        # Find and update or insert slot
        found = False
        for i, slot in enumerate(folder_slots):
            if slot.get("port") == port:
                folder_slots[i] = {"port": port, "files": files}
                found = True
                break
        if not found:
            folder_slots.append({"port": port, "files": files})

        playlists[folder_name] = folder_slots
        update_config({"streamer": {"playlists": playlists}})
        self.cleanup_playlists()
        if self.content_folder:
            self.scan_content_folder(self.content_folder)
        return True

    def remove_file_from_slot(self, folder_name: str, port: int, filename: str) -> bool:
        """Remove a file from a port slot and return to converter if unused."""
        cfg = load_config()
        playlists = dict(cfg.streamer.playlists)
        folder_slots = list(playlists.get(folder_name, []))

        for i, slot in enumerate(folder_slots):
            if slot.get("port") == port:
                new_files = [f for f in slot.get("files", []) if f != filename]
                folder_slots[i] = {"port": port, "files": new_files}
                break

        playlists[folder_name] = folder_slots
        update_config({"streamer": {"playlists": playlists}})
        self.cleanup_playlists()

        # Check if filename is still used in any slot of this folder
        still_used = any(
            filename in s.get("files", [])
            for s in playlists.get(folder_name, [])
        )
        if not still_used and self.content_folder:
            from app.converter import converter
            source_file = Path(self.content_folder) / folder_name / filename
            if source_file.exists() and converter.source_folder:
                target_file = Path(converter.source_folder) / filename
                try:
                    import shutil
                    shutil.move(str(source_file), str(target_file))
                    logger.info(f"Returned {filename} from {folder_name} to converter input folder")
                    converter.scan_folder(converter.source_folder)
                except Exception as e:
                    logger.error(f"Failed to return {filename} to converter folder: {e}")
            self.scan_content_folder(self.content_folder)
        return True

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
                "slot_files": info.slot_files,
                "current_file_index": info.current_file_index,
                "port": info.port,
                "rtmp_url": info.rtmp_url,
                "stream_url": info.stream_url,
                "status": info.status,
                "error": info.error,
                "progress": round(info.progress, 3),
                "metadata": info.metadata,
            })

        return {
            "is_running": self.is_running,
            "current_folder": self._current_folder_name,
            "active_streams": streams,
            "port_range": f"{self.port_range_start}-{self.port_range_end}",
            "errors": self._errors[-10:],
        }

    # ---- Internal methods ----

    def _find_folder(self, name: str) -> Optional[DateRangeFolder]:
        """Find a parsed folder by name."""
        for f in self.folders:
            if f.name == name:
                return f
        return None

    def _load_slots_for_folder(self, folder: DateRangeFolder) -> List[PortSlot]:
        """Load and resolve slot configuration for a folder, fetching durations."""
        self.cleanup_playlists()
        cfg = load_config()
        folder_path = Path(folder.path)
        available_ports = list(range(self.port_range_start, self.port_range_end + 1))
        slots_cfg = cfg.streamer.playlists.get(folder.name, [])

        slots: List[PortSlot] = []

        if slots_cfg:
            # Use configured slots
            for slot_entry in slots_cfg:
                port = slot_entry.get("port")
                if port is None or port not in available_ports:
                    continue
                filenames = slot_entry.get("files", [])
                paths = []
                durations = []
                valid_files = []
                for fname in filenames:
                    fpath = (folder_path / fname).resolve()
                    if not fpath.exists():
                        logger.warning(f"Slot file not found: {fpath}")
                        continue
                    meta = get_video_metadata(str(fpath))
                    dur = float(meta.get("duration", 0))
                    paths.append(str(fpath))
                    durations.append(dur)
                    valid_files.append(fname)
                if valid_files:
                    slots.append(PortSlot(port=port, files=valid_files, paths=paths, durations=durations))
        else:
            # Fallback: auto-assign files to ports alphabetically
            for i, fname in enumerate(folder.files):
                if i >= len(available_ports):
                    break
                port = available_ports[i]
                fpath = (folder_path / fname).resolve()
                if not fpath.exists():
                    continue
                meta = get_video_metadata(str(fpath))
                dur = float(meta.get("duration", 0))
                slots.append(PortSlot(port=port, files=[fname], paths=[str(fpath)], durations=[dur]))

        slots.sort(key=lambda s: s.port)
        return slots

    def _compute_slot_seek(self, slot: PortSlot, folder_start_date: date) -> Tuple[int, float]:
        """
        Compute which file and seek offset in that file to start from,
        based on how much time has elapsed since folder start_date 00:00.
        Returns (file_index, seek_offset_seconds).
        """
        now = datetime.now()
        start_dt = datetime(folder_start_date.year, folder_start_date.month, folder_start_date.day, 0, 0, 0)
        elapsed = max(0.0, (now - start_dt).total_seconds())

        cycle = sum(slot.durations)
        if cycle <= 0:
            return 0, 0.0

        pos = elapsed % cycle

        # Walk through files to find where in the cycle we are
        acc = 0.0
        for i, dur in enumerate(slot.durations):
            if pos < acc + dur:
                seek_within_file = pos - acc
                return i, seek_within_file
            acc += dur

        return 0, 0.0

    def _build_ffmpeg_concat_playlist(self, slot: PortSlot, start_file_index: int, seek_offset: float, folder: DateRangeFolder) -> str:
        """
        Build an FFmpeg concat demuxer playlist .txt file that:
        - Starts at start_file_index with seek_offset
        - Repeats the full file list enough times to cover the rest of the date range
        Returns the path to the .txt file.
        """
        now = datetime.now()
        end_dt = datetime(folder.end_date.year, folder.end_date.month, folder.end_date.day, 0, 0, 0)
        remaining_seconds = max(3600, (end_dt - now).total_seconds() + 86400)  # at least 1h, cover to end

        cycle = sum(slot.durations)
        repeats_needed = max(2, int(remaining_seconds / cycle) + 2) if cycle > 0 else 4

        lines = ["ffconcat version 1.0"]

        # First entry: seek into the correct file
        first_path = Path(slot.paths[start_file_index]).resolve().as_posix()
        lines.append(f"file '{first_path}'")
        if seek_offset > 1.0:
            lines.append(f"inpoint {seek_offset:.3f}")

        # Append remaining files in this cycle
        for j in range(start_file_index + 1, len(slot.files)):
            p = Path(slot.paths[j]).resolve().as_posix()
            lines.append(f"file '{p}'")

        # Append full cycles to cover remaining date range
        for _ in range(repeats_needed):
            for p_raw in slot.paths:
                p = Path(p_raw).resolve().as_posix()
                lines.append(f"file '{p}'")

        config_dir = Path(tempfile.gettempdir()) / "commandcenter"
        config_dir.mkdir(parents=True, exist_ok=True)
        playlist_path = config_dir / f"concat_{slot.port}.txt"
        playlist_path.write_text("\n".join(lines), encoding="utf-8")
        return str(playlist_path)

    async def _scheduler_loop(self):
        """Main scheduler loop — checks date and manages folder transitions."""
        try:
            while self.is_running:
                today = date.today()
                active_folder = self._get_active_folder(today)

                if active_folder is None:
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
                    logger.info(f"Switching to folder: {active_folder.name}")

                    await self._stop_all_streams()
                    await self._start_folder_streams(active_folder)
                    self._current_folder_name = active_folder.name

                await self._health_check()
                await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled.")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            self._errors.append(f"Scheduler error: {e}")
            self.is_running = False

    async def _start_folder_streams(self, folder: DateRangeFolder):
        """Start streaming all slots in a folder, one MediaMTX+FFmpeg per port."""
        slots = self._load_slots_for_folder(folder)

        if not slots:
            logger.warning(f"No streamable files found in folder '{folder.name}'")
            return

        for slot in slots:
            await self._start_slot_stream(slot, folder)

    async def _start_slot_stream(self, slot: PortSlot, folder: DateRangeFolder):
        """Start MediaMTX + FFmpeg for a slot (one port, multiple files round-robin)."""
        port = slot.port
        cfg = load_config()
        protocol = cfg.streamer.protocol.lower()

        if protocol == "hls":
            public_port = port
            internal_rtmp_port = port + 6000
            stream_url = f"http://127.0.0.1:{public_port}/stream/index.m3u8"
        else:
            public_port = port
            internal_rtmp_port = port
            stream_url = f"rtmp://127.0.0.1:{public_port}/stream"

        ingest_url = f"rtmp://127.0.0.1:{internal_rtmp_port}/stream"

        # Compute time-correct start position
        file_index, seek_offset = self._compute_slot_seek(slot, folder.start_date)

        logger.info(
            f"Slot port {port}: starting file #{file_index} '{slot.files[file_index]}' "
            f"at offset {seek_offset:.1f}s"
        )

        # Build concat playlist
        playlist_path = self._build_ffmpeg_concat_playlist(slot, file_index, seek_offset, folder)

        # Primary metadata = first file
        metadata = get_video_metadata(slot.paths[0]) if slot.paths else {}

        # Compute cycle start offset (duration of all previous files + seek offset)
        cum_dur = sum(slot.durations[:file_index])
        cycle_start_offset = cum_dur + seek_offset

        stream_info = StreamInfo(
            port=port,
            slot_files=list(slot.files),
            slot_paths=list(slot.paths),
            slot_durations=list(slot.durations),
            rtmp_url=ingest_url,
            stream_url=stream_url,
            status="starting",
            current_file_index=file_index,
            start_offset=seek_offset,
            cycle_start_offset=cycle_start_offset,
            metadata=metadata,
        )
        self.active_streams[port] = stream_info

        try:
            # 1. Start MediaMTX
            mtx_config = self._create_mediamtx_config(internal_rtmp_port, public_port, protocol)
            mtx_process = subprocess.Popen(
                [get_mediamtx_path(), mtx_config],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stream_info.mediamtx_process = mtx_process

            await asyncio.sleep(2)

            if mtx_process.poll() is not None:
                stderr = mtx_process.stderr.read().decode("utf-8", errors="replace") if mtx_process.stderr else ""
                stream_info.status = "error"
                stream_info.error = f"MediaMTX failed to start on port {port}: {stderr[-300:]}"
                logger.error(stream_info.error)
                self._errors.append(stream_info.error)
                return

            # 2. Start FFmpeg using concat demuxer
            ffmpeg_cmd = [
                get_ffmpeg_path(),
                "-re",                         # Read at native frame rate
                "-f", "concat",                # Concat demuxer
                "-safe", "0",
                "-i", playlist_path,
                "-c", "copy",
                "-f", "flv",
                ingest_url,
            ]

            ffmpeg_process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stream_info.ffmpeg_process = ffmpeg_process

            await asyncio.sleep(1)

            if ffmpeg_process.returncode is not None:
                stderr_data = await ffmpeg_process.stderr.read()
                stderr = stderr_data.decode("utf-8", errors="replace")
                stream_info.status = "error"
                stream_info.error = f"FFmpeg failed to start: {stderr[-300:]}"
                logger.error(stream_info.error)
                self._errors.append(stream_info.error)
                return

            stream_info.status = "live"
            logger.info(f"Stream live on port {public_port} ({protocol.upper()}) — {len(slot.files)} file(s) in slot")

            # Background log reader to track progress
            stream_info.log_task = asyncio.create_task(
                self._read_ffmpeg_logs(stream_info, ffmpeg_process.stderr)
            )

        except Exception as e:
            stream_info.status = "error"
            stream_info.error = str(e)
            logger.error(f"Failed to start slot stream on port {port}: {e}")
            self._errors.append(str(e))

    def _create_mediamtx_config(self, internal_rtmp_port: int, public_port: int, protocol: str) -> str:
        """Create a temporary MediaMTX YAML config for a specific RTMP port and target protocol."""
        hls_block = "hls: no"
        if protocol == "hls":
            hls_block = f"hls: yes\nhlsAddress: :{public_port}\nhlsAlwaysRemux: yes\nhlsVariant: lowLatency"

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
  stream:
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

        if stream.log_task:
            stream.log_task.cancel()

        if stream.ffmpeg_process and stream.ffmpeg_process.returncode is None:
            try:
                stream.ffmpeg_process.terminate()
                try:
                    await asyncio.wait_for(stream.ffmpeg_process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    stream.ffmpeg_process.kill()
            except Exception as e:
                logger.warning(f"Error stopping FFmpeg on port {port}: {e}")

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

            folder = self._find_folder(self._current_folder_name)
            if not folder:
                continue

            if stream.ffmpeg_process and stream.ffmpeg_process.returncode is not None:
                logger.warning(f"FFmpeg died on port {port}, restarting...")
                await self._stop_single_stream(port)
                slots = self._load_slots_for_folder(folder)
                slot = next((s for s in slots if s.port == port), None)
                if slot:
                    await self._start_slot_stream(slot, folder)

            elif stream.mediamtx_process and stream.mediamtx_process.poll() is not None:
                logger.warning(f"MediaMTX died on port {port}, restarting...")
                await self._stop_single_stream(port)
                slots = self._load_slots_for_folder(folder)
                slot = next((s for s in slots if s.port == port), None)
                if slot:
                    await self._start_slot_stream(slot, folder)

    async def _read_ffmpeg_logs(self, stream_info: StreamInfo, stderr_stream):
        """Read FFmpeg stderr to calculate streaming progress across the slot's files."""
        cycle_duration = sum(stream_info.slot_durations)
        buffer = ""
        try:
            while True:
                chunk = await stderr_stream.read(4096)
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")

                if '\r' in buffer or '\n' in buffer:
                    lines = buffer.replace('\r', '\n').split('\n')
                    buffer = lines.pop()

                    for line in lines:
                        match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                        if match and cycle_duration > 0:
                            # ffmpeg time= is time since concat start (already seeked)
                            ffmpeg_elapsed = (
                                int(match.group(1)) * 3600
                                + int(match.group(2)) * 60
                                + float(match.group(3))
                            )
                            # Add the initial cycle start offset to get true cycle position
                            true_pos = (stream_info.cycle_start_offset + ffmpeg_elapsed) % cycle_duration
                            stream_info.progress = true_pos / cycle_duration

                            # Update current_file_index
                            acc = 0.0
                            for i, dur in enumerate(stream_info.slot_durations):
                                if true_pos < acc + dur:
                                    stream_info.current_file_index = i
                                    break
                                acc += dur

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading ffmpeg logs for port {stream_info.port}: {e}")

    def _get_active_folder(self, today: date) -> Optional[DateRangeFolder]:
        """Find the folder whose date range includes today. If overlap, nearest start_date wins."""
        candidates = [f for f in self.folders if f.start_date <= today <= f.end_date]

        if not candidates:
            return None

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