"""Live relay stream + global VPN endpoints."""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import load_config, update_config
from app.live_relay import live_relay_manager

logger = logging.getLogger("commandcenter")
router = APIRouter()


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


@router.get("/api/streamer/live_streams")
async def get_live_streams():
    """List all configured live relay streams with status."""
    return {"live_streams": live_relay_manager.get_all_status()}


@router.get("/api/streamer/live_stream/{stream_id}/thumbnail")
async def live_stream_thumbnail(stream_id: str):
    """Get the cached thumbnail for a live stream."""
    from app.thumbnails import THUMBNAILS_DIR
    thumb_path = THUMBNAILS_DIR / f"live_{stream_id}.jpg"
    if thumb_path.exists():
        return FileResponse(str(thumb_path))
    raise HTTPException(status_code=404, detail="Thumbnail not found")


def validate_vpn_payload(mode: str, profile_content: str):
    if mode == "wireguard" and not (profile_content and profile_content.strip()):
        raise HTTPException(
            status_code=400,
            detail="WireGuard (.conf) mode requires a valid profile file."
        )


def sanitize_vpn_data(mode: str, name: str, content: str):
    """Ensure profile name and content are cleaned and cleared when mode changes."""
    if mode == "wireguard":
        if content:
            content = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    else:
        name = ""
        content = ""
    return name, content


@router.get("/api/vpn/global")
async def get_global_vpn():
    """Get global VPN configuration."""
    cfg = load_config()
    return getattr(cfg.streamer, "global_vpn", {}) or {}


@router.put("/api/vpn/global")
async def update_global_vpn(body: GlobalVPNUpdateRequest):
    """Update global VPN configuration."""
    p_name, p_content = sanitize_vpn_data(
        body.mode,
        body.profile_name or "",
        body.profile_content or ""
    )
    validate_vpn_payload(body.mode, p_content)
    new_vpn = {
        "mode": body.mode,
        "profile_name": p_name,
        "profile_content": p_content,
    }
    update_config({"streamer": {"global_vpn": new_vpn}})
    from app.vpn_manager import vpn_manager
    vpn_manager.start_global_vpn()
    return {"status": "success", "global_vpn": new_vpn}


@router.post("/api/streamer/live_stream")
async def create_live_stream(body: LiveStreamCreateRequest):
    """Create a new live relay stream."""
    import uuid
    from app.routers.common import check_port_conflict
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


@router.put("/api/streamer/live_stream/{stream_id}")
async def update_live_stream(stream_id: str, body: LiveStreamUpdateRequest):
    """Update an existing live relay stream."""
    from app.routers.common import check_port_conflict
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



@router.post("/api/streamer/live_stream/{stream_id}/start_browser")
async def start_web_stream_browser(stream_id: str):
    """Launch browser instance for a web stream."""
    try:
        res = await live_relay_manager.start_browser_for_stream(stream_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/streamer/live_stream/{stream_id}")
async def delete_live_stream(stream_id: str):
    """Delete a live relay stream."""
    await live_relay_manager.stop_stream(stream_id)
    cfg = load_config()
    cfg.streamer.live_streams = [x for x in cfg.streamer.live_streams if x.get("id") != stream_id]
    update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})
    return {"status": "success"}


@router.post("/api/streamer/live_stream/{stream_id}/start")
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


@router.post("/api/streamer/live_stream/{stream_id}/stop")
async def stop_live_stream(stream_id: str):
    """Stop the FFmpeg relay process for a live stream."""
    cfg = load_config()
    for item in cfg.streamer.live_streams:
        if item.get("id") == stream_id:
            item["auto_start"] = False
            update_config({"streamer": {"live_streams": cfg.streamer.live_streams}})
            break
    res = await live_relay_manager.stop_stream(stream_id)
    return {"status": "success", "live_stream": res}


@router.get("/api/streamer/live_stream/{stream_id}/logs")
async def get_live_stream_logs(stream_id: str):
    """Retrieve runtime stderr logs and error state for a live relay stream."""
    relay = live_relay_manager.active_relays.get(stream_id)
    if not relay:
        return {"id": stream_id, "status": "not_active", "error": None, "logs": []}
    return {
        "id": stream_id,
        "name": relay.name,
        "status": relay.status,
        "error": relay.error,
        "fps": relay.fps,
        "bitrate": relay.bitrate,
        "client_count": len(relay.clients),
        "logs": relay.last_logs[-30:],
    }
