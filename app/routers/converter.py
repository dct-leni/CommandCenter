"""Converter endpoints."""
import asyncio
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import load_config, update_config
from app.converter import converter
from app.streamer import streamer
from app.thumbnails import generate_thumbnail

logger = logging.getLogger("commandcenter")
router = APIRouter()


class FolderPath(BaseModel):
    path: str


class ConvertRequest(BaseModel):
    filename: Optional[str] = None  # None = convert all


class MoveFileRequest(BaseModel):
    filename: str
    target_folder: str


@router.post("/api/converter/scan")
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


@router.get("/api/converter/status")
async def converter_status():
    """Get conversion status for all files."""
    return {
        "folder": converter.source_folder,
        "files": converter.get_status(),
    }


@router.post("/api/converter/convert")
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


@router.post("/api/converter/stop")
async def converter_stop():
    """Stop any active conversions and clear the queue."""
    success = await converter.stop_conversion()
    if not success:
        raise HTTPException(status_code=500, detail="Failed to stop conversion")
    return {"status": "stopped"}


@router.post("/api/converter/clear-done")
async def converter_clear_done():
    """Drop completed entries from the in-memory file list."""
    done = [name for name, info in converter.files.items() if info.status == "done"]
    for name in done:
        del converter.files[name]
    return {"status": "ok", "removed": len(done)}

@router.post("/api/converter/upload")
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

        dest = Path(folder) / Path(upload.filename).name
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
    files_list = await asyncio.get_running_loop().run_in_executor(
        None, converter.scan_folder, folder)
    return {
        "saved": saved,
        "skipped": skipped,
        "files": files_list,
        "count": len(files_list),
    }


@router.get("/api/converter/thumbnail/{filename}")
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

    target_path = Path(filepath).resolve()
    source_root = Path(converter.source_folder).resolve()
    if not target_path.is_relative_to(source_root) or not target_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    thumb = await asyncio.get_running_loop().run_in_executor(
        None, generate_thumbnail, str(target_path))
    if not thumb or not Path(thumb).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(thumb, media_type="image/jpeg")


@router.post("/api/converter/move")
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
