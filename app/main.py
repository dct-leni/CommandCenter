"""
CommandCenter — FastAPI application entry point.
Serves the Web UI and provides REST API for converter and streamer.
"""

import logging
import os
import signal
import atexit
import asyncio
from dataclasses import asdict
from pathlib import Path
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from app.config import load_config, update_config, save_config, LiveStreamItem
from app.ffmpeg_setup import get_binaries_status
from app.converter import converter
from app.streamer import streamer
from app.thumbnails import get_thumbnail_path, generate_thumbnail
from app.epg import generate_epg
from app.live_relay import live_relay_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("commandcenter")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Purge any leftover temporary VPN config files and browser profiles on startup
    from app.vpn_manager import vpn_manager
    from app.web_stream import web_stream_manager
    vpn_manager.purge_temp_dir()
    web_stream_manager.purge_all()
    # Resolve external IP on app start
    asyncio.create_task(streamer._resolve_external_ip())
    # Start Global VPN on app startup if configured
    vpn_manager.start_global_vpn()

    # Populate configured folders and handle auto-resume on boot
    cfg = load_config()
    if cfg.streamer.content_folder and Path(cfg.streamer.content_folder).is_dir():
        streamer.scan_content_folder(cfg.streamer.content_folder)
        if cfg.streamer.auto_resume:
            logger.info(f"Auto-resume enabled, starting stream for folder: {cfg.streamer.content_folder}")
            asyncio.create_task(
                streamer.start_streaming(cfg.streamer.port_range_start, cfg.streamer.port_range_end)
            )
        
    # Resume auto_start live streams across server restarts (excluding web streams)
    for ls_item in cfg.streamer.live_streams:
        if ls_item.get("auto_start") and ls_item.get("stream_type") != "web":
            try:
                logger.info(f"Auto-resuming live relay stream: {ls_item.get('name')} on :{ls_item.get('port')}")
                asyncio.create_task(live_relay_manager.start_stream(ls_item.get("id")))
            except Exception as e:
                logger.error(f"Failed to auto-resume live stream {ls_item.get('id')}: {e}")

    yield
    
    if streamer.is_running:
        # Pass is_shutdown=True to prevent wiping auto_resume state on restart
        await streamer.stop_streaming(is_shutdown=True)

    for ls_status in list(live_relay_manager.active_relays.values()):
        await live_relay_manager.stop_stream(ls_status.id)

    vpn_manager.purge_temp_dir()
    web_stream_manager.purge_all()

# Create FastAPI app
app = FastAPI(title="CommandCenter", version="1.0.0", lifespan=lifespan)

# Static files (Web UI)
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────
#  Pydantic models for request bodies
# ──────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    converter: Optional[dict] = None
    streamer: Optional[dict] = None
    server: Optional[dict] = None


class FolderPath(BaseModel):
    path: str


class ConvertRequest(BaseModel):
    filename: Optional[str] = None  # None = convert all


class StreamStartRequest(BaseModel):
    port_range_start: Optional[int] = None
    port_range_end: Optional[int] = None
    protocol: Optional[str] = None


class MoveFileRequest(BaseModel):
    filename: str
    target_folder: str


class SlotUpdate(BaseModel):
    port: int
    files: List[str]


class SlotRemoveFile(BaseModel):
    port: int
    filename: str


class FolderCreateRequest(BaseModel):
    name: str


class FolderModifyRequest(BaseModel):
    new_name: str


class LiveStreamCreateRequest(BaseModel):
    name: str
    url: str
    port: int
    auto_start: bool = False
    use_vpn: bool = False
    stream_type: str = "http"  # "http" or "web"


class LiveStreamUpdateRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    port: Optional[int] = None
    auto_start: Optional[bool] = None
    use_vpn: Optional[bool] = None
    stream_type: Optional[str] = None


class GlobalVPNUpdateRequest(BaseModel):
    mode: str = "none"
    profile_name: Optional[str] = None
    profile_content: Optional[str] = None
    proxy_url: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


# ──────────────────────────────────────────────
#  Root — serve index.html
# ──────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ──────────────────────────────────────────────
#  Port Conflict Validation Helpers
# ──────────────────────────────────────────────

def check_port_conflict(port: int, cfg, ignore_live_stream_id: Optional[str] = None):
    """Verify that a requested livestream port does not cross folder stream slots or other livestreams."""
    if cfg.streamer.port_range_start <= port <= cfg.streamer.port_range_end:
        raise HTTPException(
            status_code=400,
            detail=f"Port {port} falls within the folder stream port range ({cfg.streamer.port_range_start}-{cfg.streamer.port_range_end}). Livestream and folder stream ports cannot cross."
        )

    for folder_name, slots in cfg.streamer.playlists.items():
        for slot in slots:
            if slot.get("port") == port:
                raise HTTPException(
                    status_code=400,
                    detail=f"Port {port} is already assigned to a folder stream slot in folder '{folder_name}'. Livestream and folder stream ports cannot cross."
                )

    for item in cfg.streamer.live_streams:
        if item.get("id") != ignore_live_stream_id and item.get("port") == port:
            raise HTTPException(
                status_code=400,
                detail=f"Port {port} is already in use by live stream '{item.get('name', item.get('id'))}'."
            )


def check_port_range_conflict(port_start: int, port_end: int, cfg):
    """Verify that a requested folder stream port range does not cross existing livestreams."""
    if port_start > port_end:
        raise HTTPException(status_code=400, detail="Invalid port range: start port cannot be greater than end port.")

    for item in cfg.streamer.live_streams:
        ls_port = item.get("port")
        if ls_port is not None and port_start <= ls_port <= port_end:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot set folder stream port range ({port_start}-{port_end}): Port {ls_port} is currently assigned to live stream '{item.get('name', item.get('id'))}'. Livestream and folder stream ports cannot cross."
            )


# ──────────────────────────────────────────────
#  Config endpoints
# ──────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    cfg = load_config()
    return asdict(cfg)


@app.put("/api/config")
async def put_config(body: ConfigUpdate):
    updates = body.model_dump(exclude_none=True)
    if streamer.is_running and "streamer" in updates:
        s_updates = updates["streamer"]
        cfg = load_config()
        if "protocol" in s_updates and s_updates["protocol"] != cfg.streamer.protocol:
            raise HTTPException(status_code=400, detail="Cannot modify protocol while streaming is in progress. Stop streaming first.")
        if "port_range_start" in s_updates and s_updates["port_range_start"] != cfg.streamer.port_range_start:
            raise HTTPException(status_code=400, detail="Cannot modify port range while streaming is in progress. Stop streaming first.")
        if "port_range_end" in s_updates and s_updates["port_range_end"] != cfg.streamer.port_range_end:
            raise HTTPException(status_code=400, detail="Cannot modify port range while streaming is in progress. Stop streaming first.")
        if "content_folder" in s_updates and s_updates["content_folder"].strip() != cfg.streamer.content_folder:
            raise HTTPException(status_code=400, detail="Cannot modify streams folder while streaming is in progress. Stop streaming first.")
    if "streamer" in updates:
        cfg = load_config()
        s_updates = updates["streamer"]
        new_start = s_updates.get("port_range_start", cfg.streamer.port_range_start)
        new_end = s_updates.get("port_range_end", cfg.streamer.port_range_end)
        if new_start != cfg.streamer.port_range_start or new_end != cfg.streamer.port_range_end:
            check_port_range_conflict(new_start, new_end, cfg)

    cfg = update_config(updates)
    if "streamer" in updates:
        streamer.cleanup_playlists()
        cfg = load_config()
    return asdict(cfg)


# ──────────────────────────────────────────────
#  System endpoints
# ──────────────────────────────────────────────

@app.get("/api/system/status")
async def system_status():
    status = get_binaries_status()
    from app.vpn_manager import vpn_manager
    status["vpn"] = vpn_manager.get_status()
    return status


# ──────────────────────────────────────────────
#  Converter endpoints
# ──────────────────────────────────────────────

@app.post("/api/converter/scan")
async def converter_scan(body: FolderPath):
    """Scan a folder for video files."""
    path = body.path.strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(status_code=400, detail="Invalid folder path")

    # Save to config if changed
    cfg = load_config()
    if cfg.converter.source_folder != path:
        update_config({"converter": {"source_folder": path}})

    files = converter.scan_folder(path)
    return {"folder": path, "files": files, "count": len(files)}


@app.get("/api/converter/status")
async def converter_status():
    """Get conversion status for all files."""
    return {
        "folder": converter.source_folder,
        "files": converter.get_status(),
    }


@app.post("/api/converter/convert")
async def converter_convert(body: ConvertRequest):
    """Start converting a file (or all pending files)."""
    if body.filename:
        success = await converter.convert_file(body.filename)
        if not success:
            raise HTTPException(status_code=400, detail="Conversion failed to start")
        return {"status": "started", "filename": body.filename}
    else:
        count = await converter.convert_all()
        return {"status": "started", "count": count}


@app.post("/api/converter/stop")
async def converter_stop():
    """Stop any active conversions and clear the queue."""
    success = await converter.stop_conversion()
    if not success:
        raise HTTPException(status_code=500, detail="Failed to stop conversion")
    return {"status": "stopped"}


@app.post("/api/converter/upload")
async def converter_upload(files: List[UploadFile] = File(...)):
    """
    Receive video files from the browser (drag & drop) and save them into
    the configured converter source_folder.
    Works for both local and remote server instances — the browser streams
    the file over HTTP regardless of where the server runs.
    """
    folder = converter.source_folder
    if not folder or not Path(folder).is_dir():
        raise HTTPException(
            status_code=400,
            detail="No input folder selected. Please choose a source folder first."
        )

    from app.converter import VIDEO_EXTENSIONS
    saved = []
    skipped = []

    for upload in files:
        ext = Path(upload.filename).suffix.lower()
        if ext not in VIDEO_EXTENSIONS:
            skipped.append(upload.filename)
            continue

        dest = Path(folder) / upload.filename
        # Stream in 1 MB chunks to keep memory usage flat for large files
        try:
            with open(dest, "wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            saved.append(upload.filename)
        except Exception as e:
            logger.error(f"Failed to save uploaded file '{upload.filename}': {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save '{upload.filename}': {e}")

    # Rescan so uploaded files appear immediately
    files_list = converter.scan_folder(folder)
    return {
        "saved": saved,
        "skipped": skipped,
        "files": files_list,
        "count": len(files_list),
    }


@app.get("/api/converter/thumbnail/{filename}")
async def converter_thumbnail(filename: str):
    """Get thumbnail for a file in the converter folder."""
    if filename not in converter.files:
        raise HTTPException(status_code=404, detail="File not found")

    info = converter.files[filename]
    filepath = info.filepath

    # If file was renamed to .original, try the .ts version
    if filepath.endswith(".original"):
        ts_path = str(Path(converter.source_folder) / info.ts_filename)
        if Path(ts_path).exists():
            filepath = ts_path

    thumb = generate_thumbnail(filepath)
    if not thumb or not Path(thumb).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(thumb, media_type="image/jpeg")


@app.post("/api/converter/move")
async def converter_move(body: MoveFileRequest):
    """Move a file from the converter folder to a streamer folder."""
    if body.filename not in converter.files:
        raise HTTPException(status_code=404, detail="File not found in converter")

    info = converter.files[body.filename]
    
    if info.status != "done" and info.extension != ".ts":
        raise HTTPException(status_code=400, detail="Only converted (.ts) files can be moved")

    source_path = Path(info.filepath)
    
    # Check if we should move the .ts file or the original
    if info.status == "done" and info.ts_filename:
        ts_path = Path(converter.source_folder) / info.ts_filename
        if ts_path.exists():
            source_path = ts_path

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source file not found on disk")

    target_dir = Path(streamer.content_folder) / body.target_folder
    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Target folder not found")

    target_path = target_dir / source_path.name
    
    import shutil
    try:
        await asyncio.to_thread(shutil.move, str(source_path), str(target_path))
        # Update converter state
        converter.scan_folder(converter.source_folder)
        # Update streamer state
        streamer.scan_content_folder(streamer.content_folder)
        return {"status": "success", "message": f"Moved to {body.target_folder}"}
    except Exception as e:
        logger.error(f"Failed to move file: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ──────────────────────────────────────────────
#  Streamer endpoints
# ──────────────────────────────────────────────

@app.post("/api/streamer/scan")
async def streamer_scan(body: FolderPath):
    """Scan root folder for date-range subfolders."""
    path = body.path.strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(status_code=400, detail="Invalid folder path")

    if streamer.is_running and path != streamer.content_folder:
        raise HTTPException(
            status_code=400,
            detail="Cannot change streams folder while streaming is in progress. Stop streaming first."
        )

    # Save to config if changed
    cfg = load_config()
    if cfg.streamer.content_folder != path:
        update_config({"streamer": {"content_folder": path}})

    folders = streamer.scan_content_folder(path)
    return {"folder": path, "folders": folders, "count": len(folders)}


@app.get("/api/streamer/folders")
async def streamer_folders():
    """Get list of scanned date-range folders."""
    return {
        "folder": streamer.content_folder,
        "folders": [streamer._folder_to_dict(f) for f in streamer.folders],
    }


@app.get("/api/streamer/folder/{name}")
async def streamer_folder_detail(name: str):
    """Get detailed info about a specific folder, including slot configuration."""
    detail = streamer.get_folder_details(name)
    if not detail:
        raise HTTPException(status_code=404, detail="Folder not found")
    return detail


@app.post("/api/streamer/folders")
async def streamer_create_folder(body: FolderCreateRequest):
    """Create a new date-range folder inside the content folder."""
    try:
        folders = streamer.create_folder(body.name)
        return {"status": "ok", "name": body.name, "folders": folders}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/streamer/folder/{folder_name}")
async def streamer_modify_folder(folder_name: str, body: FolderModifyRequest):
    """Modify (rename) an existing date-range folder."""
    try:
        folders = streamer.rename_folder(folder_name, body.new_name)
        return {"status": "ok", "old_name": folder_name, "new_name": body.new_name, "folders": folders}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to modify folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/streamer/folder/{folder_name}")
async def streamer_delete_folder(folder_name: str):
    """Delete a non-active folder and return its files to converter source folder."""
    try:
        folders = streamer.delete_folder(folder_name)
        # Rescan converter so returned files show up immediately
        from app.converter import converter
        converter.scan_configured_folder()
        return {"status": "ok", "folders": folders}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to delete folder {folder_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def generate_epg_for_folder(folder_name: str) -> Optional[str]:
    """Helper to generate EPG for a folder and return its output path."""
    folder = streamer._find_folder(folder_name)
    if not folder:
        logger.warning(f"Could not generate EPG: folder '{folder_name}' not found")
        return None

    if not folder.is_active:
        logger.info(f"Skipping EPG generation for non-active folder '{folder_name}'")
        return None

    cfg = load_config()
    
    # Skip EPG generation if there are no videos in the folder
    if not folder.files:
        logger.info(f"Skipping EPG generation for folder '{folder_name}': no video files present")
        try:
            from pathlib import Path as _Path
            epg_path = _Path(folder.path) / f"{cfg.streamer.channel_prefix.lower()}.xml"
            epg_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    lang = cfg.converter.languages[0] if cfg.converter.languages else "en"
    channel_prefix = cfg.streamer.channel_prefix
    timezone_str = cfg.streamer.epg_timezone

    # Load slots with durations
    slots_cfg = cfg.streamer.playlists.get(folder_name, [])
    from pathlib import Path as _Path
    folder_path = _Path(folder.path)

    if not slots_cfg:
        # Auto-assign fallback
        available_ports = list(range(cfg.streamer.port_range_start, cfg.streamer.port_range_end + 1))
        slots_cfg = []
        for i, fname in enumerate(folder.files):
            if i >= len(available_ports):
                break
            slots_cfg.append({"port": available_ports[i], "files": [fname]})

    # Fetch durations for each slot
    from app.thumbnails import get_video_metadata
    slots_with_durations = []
    for slot in slots_cfg:
        files = slot.get("files", [])
        durations = []
        for fname in files:
            fpath = folder_path / fname
            if fpath.exists():
                meta = get_video_metadata(str(fpath))
                durations.append(float(meta.get("duration", 3600)))
            else:
                durations.append(3600.0)
        slots_with_durations.append({
            "port": slot.get("port"),
            "files": files,
            "durations": durations,
        })

    try:
        output_path = generate_epg(
            folder_path=str(folder.path),
            slots=slots_with_durations,
            start_date=folder.start_date,
            end_date=folder.end_date,
            lang=lang,
            channel_prefix=channel_prefix,
            timezone_str=timezone_str,
            port_range_start=cfg.streamer.port_range_start,
        )
        return output_path
    except Exception as e:
        logger.error(f"Failed to generate EPG for folder {folder_name}: {e}")
        return None


@app.put("/api/streamer/folder/{folder_name}/slot")
async def streamer_update_slot(folder_name: str, body: SlotUpdate):
    """Update the file list for a specific port slot in a folder."""
    cfg = load_config()
    for item in cfg.streamer.live_streams:
        if item.get("port") == body.port:
            raise HTTPException(
                status_code=400,
                detail=f"Port {body.port} is currently assigned to live stream '{item.get('name', item.get('id'))}'. Livestream and folder stream ports cannot cross."
            )

    folder = streamer._find_folder(folder_name)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    ok = streamer.update_slot(folder_name, body.port, body.files)
    
    # Auto-generate EPG on slot configuration changes
    generate_epg_for_folder(folder_name)

    # Restart the active stream dynamically to apply new order
    if streamer.is_running and body.port in streamer.active_streams:
        slots = streamer._load_slots_for_folder(folder)
        updated_slot = next((s for s in slots if s.port == body.port), None)
        if updated_slot:
            logger.info(f"Restarting stream on port {body.port} due to slot update")
            await streamer._stop_single_stream(body.port)
            await streamer._start_slot_stream(updated_slot, folder)

    return {"status": "ok", "folder": folder_name, "port": body.port, "files": body.files}


@app.delete("/api/streamer/folder/{folder_name}/slot/file")
async def streamer_remove_slot_file(folder_name: str, body: SlotRemoveFile):
    """Remove a single file from a port slot."""
    folder = streamer._find_folder(folder_name)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    streamer.remove_file_from_slot(folder_name, body.port, body.filename)
    
    # Auto-generate EPG on file removal
    generate_epg_for_folder(folder_name)

    # Restart active stream dynamically or remove from active if no files left
    if streamer.is_running and body.port in streamer.active_streams:
        slots = streamer._load_slots_for_folder(folder)
        updated_slot = next((s for s in slots if s.port == body.port), None)
        if updated_slot:
            logger.info(f"Restarting stream on port {body.port} due to file removal")
            await streamer._stop_single_stream(body.port)
            if updated_slot.files:
                await streamer._start_slot_stream(updated_slot, folder)
            else:
                if body.port in streamer.active_streams:
                    del streamer.active_streams[body.port]

    return {"status": "ok"}


@app.post("/api/streamer/folder/{folder_name}/epg")
async def streamer_generate_epg(folder_name: str):
    """Generate an EPG XML file for a folder based on its slot configuration."""
    output_path = generate_epg_for_folder(folder_name)
    if not output_path:
        raise HTTPException(status_code=500, detail="EPG generation failed")
    from pathlib import Path as _Path
    return {"status": "ok", "path": output_path, "filename": _Path(output_path).name}


@app.get("/api/streamer/folder/{folder_name}/epg")
async def streamer_download_epg(folder_name: str):
    """Download the generated EPG XML file for a folder."""
    folder = streamer._find_folder(folder_name)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    cfg = load_config()
    channel_prefix = cfg.streamer.channel_prefix
    epg_path = Path(folder.path) / f"{channel_prefix.lower()}.xml"

    if not epg_path.exists():
        raise HTTPException(status_code=404, detail="EPG file not generated yet")

    return FileResponse(str(epg_path), media_type="application/xml", filename=epg_path.name)


@app.get("/api/streamer/folder/{folder_name}/thumbnail/{filename}")
async def streamer_thumbnail(folder_name: str, filename: str):
    """Get thumbnail for a file in a streamer folder."""
    folder = streamer._find_folder(folder_name)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    filepath = str(Path(folder.path) / filename)
    if not Path(filepath).exists():
        raise HTTPException(status_code=404, detail="File not found")

    thumb = generate_thumbnail(filepath)
    if not thumb or not Path(thumb).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(thumb, media_type="image/jpeg")


@app.post("/api/streamer/start")
async def streamer_start(body: StreamStartRequest):
    """Start the streaming scheduler."""
    cfg = load_config()
    port_start = body.port_range_start or cfg.streamer.port_range_start
    port_end = body.port_range_end or cfg.streamer.port_range_end
    protocol = body.protocol or cfg.streamer.protocol

    check_port_range_conflict(port_start, port_end, cfg)

    # Save port range, protocol and set auto_resume
    update_config({"streamer": {
        "port_range_start": port_start,
        "port_range_end": port_end,
        "protocol": protocol,
        "auto_resume": True
    }})

    result = await streamer.start_streaming(port_start, port_end)
    return result

@app.post("/api/streamer/stop")
async def streamer_stop():
    """Stop all active streams."""
    result = await streamer.stop_streaming(is_shutdown=False)
    return result


@app.get("/api/streamer/status")
async def streamer_status():
    """Get current streaming status."""
    return streamer.get_status()


# ──────────────────────────────────────────────
#  Live Relay Streams
# ──────────────────────────────────────────────

@app.get("/api/streamer/live_streams")
async def get_live_streams():
    """List all configured live relay streams with status."""
    return {"live_streams": live_relay_manager.get_all_status()}


@app.get("/api/streamer/live_stream/{stream_id}/thumbnail")
async def live_stream_thumbnail(stream_id: str):
    """Get the cached thumbnail for a live stream."""
    from app.thumbnails import THUMBNAILS_DIR
    thumb_path = THUMBNAILS_DIR / f"live_{stream_id}.jpg"
    if thumb_path.exists():
        return FileResponse(str(thumb_path))
    raise HTTPException(status_code=404, detail="Thumbnail not found")


def validate_vpn_payload(mode: str, profile_content: str, proxy_url: str):
    if mode == "wireguard" and not (profile_content and profile_content.strip()):
        raise HTTPException(
            status_code=400,
            detail="WireGuard (.conf) mode requires a valid profile file."
        )
    if mode == "proxy" and not (proxy_url and proxy_url.strip()):
        raise HTTPException(
            status_code=400,
            detail="Proxy mode requires a valid Proxy URL (e.g. socks5://127.0.0.1:1080)."
        )


def sanitize_vpn_data(mode: str, name: str, content: str, proxy: str):
    """Ensure profile name/content and proxy URL are cleaned and cleared when mode changes."""
    if mode == "wireguard":
        if content:
            content = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    else:
        name = ""
        content = ""
    if mode != "proxy":
        proxy = ""
    return name, content, proxy


@app.get("/api/vpn/global")
async def get_global_vpn():
    """Get global VPN configuration."""
    cfg = load_config()
    return getattr(cfg.streamer, "global_vpn", {}) or {}


@app.put("/api/vpn/global")
async def update_global_vpn(body: GlobalVPNUpdateRequest):
    """Update global VPN configuration."""
    p_name, p_content, p_proxy = sanitize_vpn_data(
        body.mode,
        body.profile_name or "",
        body.profile_content or "",
        body.proxy_url or ""
    )
    validate_vpn_payload(body.mode, p_content, p_proxy)
    new_vpn = {
        "mode": body.mode,
        "profile_name": p_name,
        "profile_content": p_content,
        "proxy_url": p_proxy,
        "proxy_username": body.proxy_username or "",
        "proxy_password": body.proxy_password or "",
        "vpn_username": body.vpn_username or "",
        "vpn_password": body.vpn_password or "",
    }
    update_config({"streamer": {"global_vpn": new_vpn}})
    from app.vpn_manager import vpn_manager
    vpn_manager.start_global_vpn()
    return {"status": "success", "global_vpn": new_vpn}


@app.post("/api/streamer/live_stream")
async def create_live_stream(body: LiveStreamCreateRequest):
    """Create a new live relay stream."""
    import uuid
    cfg = load_config()
    check_port_conflict(body.port, cfg)
    stream_id = f"live_{uuid.uuid4().hex[:8]}"
    new_item = {
        "id": stream_id,
        "name": body.name,
        "url": body.url,
        "port": body.port,
        "auto_start": body.auto_start,
        "use_vpn": body.use_vpn,
        "stream_type": body.stream_type or "http",
    }
    cfg.streamer.live_streams.append(new_item)
    update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})
    return {"status": "success", "live_stream": new_item}


@app.put("/api/streamer/live_stream/{stream_id}")
async def update_live_stream(stream_id: str, body: LiveStreamUpdateRequest):
    """Update an existing live relay stream."""
    cfg = load_config()
    if body.port is not None:
        check_port_conflict(body.port, cfg, ignore_live_stream_id=stream_id)
    for item in cfg.streamer.live_streams:
        if item.get("id") == stream_id:
            if body.name is not None:
                item["name"] = body.name
            if body.url is not None:
                item["url"] = body.url
            if body.port is not None:
                item["port"] = body.port
            if body.auto_start is not None:
                item["auto_start"] = body.auto_start
            if body.use_vpn is not None:
                item["use_vpn"] = body.use_vpn
            if body.stream_type is not None:
                item["stream_type"] = body.stream_type

            update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})

            if stream_id in live_relay_manager.active_relays:
                relay = live_relay_manager.active_relays[stream_id]
                relay.name = item["name"]
                relay.url = item["url"]
                relay.port = item["port"]

            return {"status": "success", "live_stream": item}
    raise HTTPException(status_code=404, detail="Live stream not found")



@app.post("/api/streamer/live_stream/{stream_id}/start_browser")
async def start_web_stream_browser(stream_id: str):
    """Launch browser instance for a web stream."""
    try:
        res = await live_relay_manager.start_browser_for_stream(stream_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/streamer/live_stream/{stream_id}")
async def delete_live_stream(stream_id: str):
    """Delete a live relay stream."""
    await live_relay_manager.stop_stream(stream_id)
    cfg = load_config()
    cfg.streamer.live_streams = [x for x in cfg.streamer.live_streams if x.get("id") != stream_id]
    update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})
    return {"status": "success"}


@app.post("/api/streamer/live_stream/{stream_id}/start")
async def start_live_stream(stream_id: str):
    """Start the FFmpeg relay process for a live stream."""
    try:
        # Dynamically set auto_start to True in config (excluding web streams) so it auto-resumes on boot
        cfg = load_config()
        for item in cfg.streamer.live_streams:
            if item.get("id") == stream_id:
                if item.get("stream_type") != "web":
                    item["auto_start"] = True
                else:
                    item["auto_start"] = False
                update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})
                break
        res = await live_relay_manager.start_stream(stream_id)
        return {"status": "success", "live_stream": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/streamer/live_stream/{stream_id}/stop")
async def stop_live_stream(stream_id: str):
    """Stop the FFmpeg relay process for a live stream."""
    # Dynamically set auto_start to False in config so it stays stopped on boot
    cfg = load_config()
    for item in cfg.streamer.live_streams:
        if item.get("id") == stream_id:
            item["auto_start"] = False
            update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})
            break
    res = await live_relay_manager.stop_stream(stream_id)
    return {"status": "success", "live_stream": res}


# ──────────────────────────────────────────────
#  Browse filesystem (for folder selection)
# ──────────────────────────────────────────────

@app.get("/api/browse")
async def browse_filesystem(path: str = Query("")):
    """Browse the filesystem to select folders."""
    if not path:
        # Return drive roots on Windows
        if os.name == "nt":
            import string
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append({"name": drive, "path": drive, "is_dir": True})
            return {"path": "", "entries": drives}
        else:
            path = "/"

    target = Path(path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=400, detail="Invalid path")

    entries = []
    try:
        for item in sorted(target.iterdir()):
            try:
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                })
            except PermissionError:
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "entries": entries,
    }


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────

def _kill_all_children():
    """
    Kill the entire process tree of this Python process (FFmpeg, MediaMTX, VPN proxies, etc.).
    Uses Windows-native `taskkill /F /T` for recursive termination.
    Called by both the console-close handler and atexit so no orphans survive.
    """
    try:
        from app.vpn_manager import vpn_manager
        vpn_manager.stop_all()
    except Exception:
        pass
    if os.name == "nt":
        import subprocess as _sp
        try:
            _sp.run(
                ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                creationflags=_sp.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    else:
        import subprocess as _sp
        try:
            _sp.run(["kill", "-TERM", f"-{os.getpid()}"], stderr=_sp.DEVNULL)
        except Exception:
            pass


def main():
    cfg = load_config()
    binaries = get_binaries_status()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║         ⚡ CommandCenter v1.0            ║")
    print("  ║    Video Converter & RTMP Streamer       ║")
    print("  ╚══════════════════════════════════════════╝")
    print()
    print(f"  FFmpeg:   {'✓ Ready' if binaries['ffmpeg'] else '✗ Missing — run setup_binaries.bat'}")
    print(f"  MediaMTX: {'✓ Ready' if binaries['mediamtx'] else '✗ Missing — run setup_binaries.bat'}")
    print(f"  Encoder:  {binaries['best_encoder']} (Auto-detected)")
    print(f"  Web UI:   http://localhost:{cfg.server.port}")
    print()

    # ── Windows console-close handler ─────────────────────────────────────
    # When the user clicks X on the console window (or logs off / shuts down),
    # Windows sends CTRL_CLOSE_EVENT. Uvicorn only handles SIGINT (Ctrl+C), so
    # the window-close leaves FFmpeg / MediaMTX orphaned.
    # We register a handler that kills the full process tree immediately.
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes
        import subprocess as _sp

        CTRL_CLOSE_EVENT    = 2
        CTRL_LOGOFF_EVENT   = 5
        CTRL_SHUTDOWN_EVENT = 6

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
        def _ctrl_handler(ctrl_type):
            if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                logger.info("Console close/logoff event — killing process tree")
                try:
                    _sp.run(
                        ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                        stdout=_sp.DEVNULL,
                        stderr=_sp.DEVNULL,
                        creationflags=_sp.CREATE_NO_WINDOW,
                    )
                except Exception:
                    pass
                return True  # suppress Windows' own delayed hard-kill
            return False  # let Ctrl+C / Ctrl+Break reach uvicorn normally

        ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler, True)
        logger.info("Registered Windows console-close handler")

    # ── atexit fallback ───────────────────────────────────────────────────
    # Runs on any clean Python exit (SIGINT → uvicorn shutdown).
    # Catches any surviving child processes lifespan teardown may have missed.
    atexit.register(_kill_all_children)

    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        use_colors=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
