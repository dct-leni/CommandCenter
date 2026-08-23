"""System endpoints: config, status WebSocket, filesystem browser."""
import asyncio
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.config import load_config, update_config
from app.ffmpeg_setup import get_binaries_status
from app.converter import converter
from app.streamer import streamer
from app.live_relay import live_relay_manager

logger = logging.getLogger("commandcenter")
router = APIRouter()


class ConfigUpdate(BaseModel):
    converter: Optional[dict] = None
    streamer: Optional[dict] = None
    server: Optional[dict] = None


@router.get("/api/config")
async def get_config():
    cfg = load_config()
    return asdict(cfg)


@router.put("/api/config")
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
            from app.routers.common import check_port_range_conflict
            check_port_range_conflict(new_start, new_end, cfg)

    cfg = update_config(updates)
    if "server" in updates and "auto_start" in updates["server"]:
        from app.autostart import set_auto_start
        if not set_auto_start(bool(updates["server"]["auto_start"])):
            raise HTTPException(status_code=500, detail="Failed to update Windows auto-start registry entry")
    if "streamer" in updates:
        streamer.cleanup_playlists()
        cfg = load_config()
    return asdict(cfg)


@router.get("/api/system/status")
async def system_status():
    status = get_binaries_status()
    from app.vpn_manager import vpn_manager
    status["vpn"] = vpn_manager.get_status()
    return status


@router.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    """Stream live converter, streamer, live relay, and system status updates over WebSocket."""
    await websocket.accept()
    from app.vpn_manager import vpn_manager
    try:
        while True:
            payload = {
                "system": get_binaries_status(),
                "vpn": vpn_manager.get_status(),
                "converter": {
                    "folder": converter.source_folder,
                    "files": converter.get_status(),
                },
                "streamer": streamer.get_status(),
                "live_streams": live_relay_manager.get_all_status(),
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket status connection closed: {e}")


@router.get("/api/browse")
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
