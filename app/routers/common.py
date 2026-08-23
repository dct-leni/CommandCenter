"""Helpers shared by multiple routers."""
from typing import Optional

from fastapi import HTTPException


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
