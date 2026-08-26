"""Streamer endpoints: date-range folders, slots, EPG, streaming scheduler."""
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import load_config, update_config
from app.streamer import streamer
from app.epg import generate_epg

logger = logging.getLogger("commandcenter")
router = APIRouter()


class FolderPath(BaseModel):
    path: str


class StreamStartRequest(BaseModel):
    port_range_start: Optional[int] = None
    port_range_end: Optional[int] = None
    protocol: Optional[str] = None


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


@router.post("/api/streamer/scan")
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


@router.get("/api/streamer/folders")
async def streamer_folders():
    """Get list of scanned date-range folders."""
    return {
        "folder": streamer.content_folder,
        "folders": [streamer._folder_to_dict(f) for f in streamer.folders],
    }


@router.get("/api/streamer/folder/{name}")
async def streamer_folder_detail(name: str):
    """Get detailed info about a specific folder, including slot configuration."""
    detail = streamer.get_folder_details(name)
    if not detail:
        raise HTTPException(status_code=404, detail="Folder not found")
    return detail


@router.post("/api/streamer/folders")
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


@router.put("/api/streamer/folder/{folder_name}")
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


@router.delete("/api/streamer/folder/{folder_name}")
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


_EPG_CACHE: dict = {}  # folder_name -> (timestamp, output_path)

def generate_epg_for_folder(folder_name: str, force: bool = False) -> Optional[str]:
    """Helper to generate EPG for a folder and return its output path."""
    import time
    if not force and folder_name in _EPG_CACHE:
        ts, cached_path = _EPG_CACHE[folder_name]
        if time.time() - ts < 60.0 and Path(cached_path).exists():
            return cached_path

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
        if output_path:
            import time
            _EPG_CACHE[folder_name] = (time.time(), output_path)
        return output_path
    except Exception as e:
        logger.error(f"Failed to generate EPG for folder {folder_name}: {e}")
        return None


@router.put("/api/streamer/folder/{folder_name}/slot")
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
    generate_epg_for_folder(folder_name, force=True)

    # Restart the active stream dynamically to apply new order
    if streamer.is_running and body.port in streamer.active_streams:
        slots = streamer._load_slots_for_folder(folder)
        updated_slot = next((s for s in slots if s.port == body.port), None)
        if updated_slot:
            logger.info(f"Restarting stream on port {body.port} due to slot update")
            await streamer._stop_single_stream(body.port)
            await streamer._start_slot_stream(updated_slot, folder)

    return {"status": "ok", "folder": folder_name, "port": body.port, "files": body.files}


@router.delete("/api/streamer/folder/{folder_name}/slot/file")
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


@router.post("/api/streamer/folder/{folder_name}/epg")
async def streamer_generate_epg(folder_name: str):
    """Generate an EPG XML file for a folder based on its slot configuration."""
    output_path = generate_epg_for_folder(folder_name)
    if not output_path:
        raise HTTPException(status_code=500, detail="EPG generation failed")
    from pathlib import Path as _Path
    return {"status": "ok", "path": output_path, "filename": _Path(output_path).name}


@router.get("/api/streamer/folder/{folder_name}/epg")
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


@router.get("/api/streamer/folder/{folder_name}/thumbnail/{filename}")
async def streamer_thumbnail(folder_name: str, filename: str):
    """Get thumbnail for a file in a streamer folder."""
    import asyncio

    from app.thumbnails import generate_thumbnail
    folder = streamer._find_folder(folder_name)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder_path = Path(folder.path).resolve()
    target_path = (folder_path / filename).resolve()
    if not target_path.is_relative_to(folder_path) or not target_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    thumb = await asyncio.get_running_loop().run_in_executor(
        None, generate_thumbnail, str(target_path))
    if not thumb or not Path(thumb).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(thumb, media_type="image/jpeg")


@router.post("/api/streamer/start")
async def streamer_start(body: StreamStartRequest):
    """Start the streaming scheduler."""
    from app.routers.common import check_port_range_conflict
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

@router.post("/api/streamer/stop")
async def streamer_stop():
    """Stop all active streams."""
    result = await streamer.stop_streaming(is_shutdown=False)
    return result


@router.get("/api/streamer/status")
async def streamer_status():
    """Get current streaming status."""
    return streamer.get_status()
