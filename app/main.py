"""
CommandCenter — FastAPI application entry point.
Serves the Web UI and provides REST API for converter and streamer.
"""

import logging
import os
from dataclasses import asdict
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from app.config import load_config, update_config, save_config
from app.ffmpeg_setup import get_binaries_status
from app.converter import converter
from app.streamer import streamer
from app.thumbnails import get_thumbnail_path, generate_thumbnail

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("commandcenter")

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if streamer.is_running:
        await streamer.stop_streaming()

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


class MoveFileRequest(BaseModel):
    filename: str
    target_folder: str


# ──────────────────────────────────────────────
#  Root — serve index.html
# ──────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


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
    cfg = update_config(updates)
    return asdict(cfg)


# ──────────────────────────────────────────────
#  System endpoints
# ──────────────────────────────────────────────

@app.get("/api/system/status")
async def system_status():
    return get_binaries_status()


# ──────────────────────────────────────────────
#  Converter endpoints
# ──────────────────────────────────────────────

@app.post("/api/converter/scan")
async def converter_scan(body: FolderPath):
    """Scan a folder for video files."""
    path = body.path.strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(status_code=400, detail="Invalid folder path")

    # Save to config
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
        shutil.move(str(source_path), str(target_path))
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

    # Save to config
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
    """Get detailed info about a specific folder."""
    detail = streamer.get_folder_details(name)
    if not detail:
        raise HTTPException(status_code=404, detail="Folder not found")
    return detail


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

    # Save port range to config
    update_config({"streamer": {
        "port_range_start": port_start,
        "port_range_end": port_end,
    }})

    result = await streamer.start_streaming(port_start, port_end)
    return result


@app.post("/api/streamer/stop")
async def streamer_stop():
    """Stop all active streams."""
    result = await streamer.stop_streaming()
    return result


@app.get("/api/streamer/status")
async def streamer_status():
    """Get current streaming status."""
    return streamer.get_status()


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
    print(f"  Web UI:   http://localhost:{cfg.server.port}")
    print()

    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        use_colors=False,
    )


if __name__ == "__main__":
    main()
