"""
Live Stream Relay Manager for CommandCenter.
Manages background FFmpeg processes to ingest HTTP/RTSP/RTMP streams,
encode them (with optional NVENC hardware acceleration), and re-stream
MPEG-TS over HTTP (-listen 1) with auto-restart loops.
"""

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.config import load_config
from app.ffmpeg_setup import get_ffmpeg_path, is_nvenc_available
from app.thumbnails import THUMBNAILS_DIR

logger = logging.getLogger(__name__)


@dataclass
class LiveRelayStatus:
    id: str
    name: str
    url: str
    port: int
    codec: str
    status: str = "stopped"  # stopped, running, listening, error
    error: Optional[str] = None
    fps: float = 0.0
    bitrate: str = "0kbits/s"
    process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    restart_task: Optional[asyncio.Task] = field(default=None, repr=False)
    log_task: Optional[asyncio.Task] = field(default=None, repr=False)
    last_logs: List[str] = field(default_factory=list, repr=False)

    @property
    def has_thumbnail(self) -> bool:
        return (THUMBNAILS_DIR / f"live_{self.id}.jpg").exists()

    @property
    def last_thumbnail_time(self) -> float:
        thumb_path = THUMBNAILS_DIR / f"live_{self.id}.jpg"
        if thumb_path.exists():
            try:
                return thumb_path.stat().st_mtime
            except Exception:
                pass
        return 0.0

    def to_dict(self) -> dict:
        from app.ffmpeg_setup import get_best_encoder
        best_encoder = get_best_encoder()
        actual_codec = best_encoder if self.codec in ("h264_nvenc", "h264_qsv") else self.codec
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "port": self.port,
            "codec": actual_codec,
            "status": self.status,
            "error": self.error,
            "fps": round(self.fps, 1),
            "bitrate": self.bitrate,
            "has_thumbnail": self.has_thumbnail,
            "thumbnail_url": f"/api/streamer/live_stream/{self.id}/thumbnail?v={int(self.last_thumbnail_time)}",
        }


_LAST_THUMBNAIL_TIME: Dict[str, float] = {}


class LiveStreamManager:
    """Singleton managing active live relay streams."""

    def __init__(self):
        self.active_relays: Dict[str, LiveRelayStatus] = {}

    def get_all_status(self) -> List[dict]:
        """Return status for all configured live streams."""
        cfg = load_config()
        results = []
        for item in cfg.streamer.live_streams:
            sid = item.get("id")
            if not sid:
                continue
            if sid in self.active_relays:
                relay = self.active_relays[sid]
                if relay.status in ("running", "listening"):
                    self.trigger_thumbnail_generation(sid, relay.port)
                results.append(relay.to_dict())
            else:
                thumb_path = THUMBNAILS_DIR / f"live_{sid}.jpg"
                has_thumb = thumb_path.exists()
                mtime = int(thumb_path.stat().st_mtime) if has_thumb else 0
                from app.ffmpeg_setup import get_best_encoder
                best_encoder = get_best_encoder()
                req_codec = item.get("codec", "h264_nvenc")
                actual_codec = best_encoder if req_codec in ("h264_nvenc", "h264_qsv") else req_codec
                results.append({
                    "id": sid,
                    "name": item.get("name", "Unnamed Stream"),
                    "url": item.get("url", ""),
                    "port": item.get("port", 1913),
                    "codec": actual_codec,
                    "status": "stopped",
                    "error": None,
                    "fps": 0.0,
                    "bitrate": "0kbits/s",
                    "has_thumbnail": has_thumb,
                    "thumbnail_url": f"/api/streamer/live_stream/{sid}/thumbnail?v={mtime}",
                })
        return results

    def trigger_thumbnail_generation(self, stream_id: str, port: int):
        """Trigger background generation of live stream thumbnail if running."""
        now = time.time()
        last_time = _LAST_THUMBNAIL_TIME.get(stream_id, 0.0)
        if now - last_time < 60.0:  # rate limit to once per 60 seconds (1 minute)
            return

        _LAST_THUMBNAIL_TIME[stream_id] = now
        
        async def task():
            try:
                local_url = f"http://127.0.0.1:{port}/"
                THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
                thumb_path = THUMBNAILS_DIR / f"live_{stream_id}.jpg"
                temp_path = THUMBNAILS_DIR / f"live_{stream_id}_temp.jpg"

                cmd = [
                    get_ffmpeg_path(),
                    "-i", local_url,
                    "-vframes", "1",
                    "-q:v", "6",
                    "-vf", "scale=120:-1",
                    "-y",
                    str(temp_path)
                ]
                
                # Start process and wait with a short timeout
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                    if temp_path.exists():
                        if thumb_path.exists():
                            thumb_path.unlink()
                        temp_path.rename(thumb_path)
                except asyncio.TimeoutError:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                finally:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Error generating thumbnail for live stream {stream_id}: {e}")

        asyncio.create_task(task())

    def get_status(self, stream_id: str) -> Optional[dict]:
        """Return status dict for a specific stream ID."""
        for item in self.get_all_status():
            if item["id"] == stream_id:
                return item
        return None

    async def start_stream(self, stream_id: str) -> dict:
        """Start or resume a live relay stream."""
        cfg = load_config()
        item = next((x for x in cfg.streamer.live_streams if x.get("id") == stream_id), None)
        if not item:
            raise ValueError(f"Live stream {stream_id} not found in configuration")

        if stream_id in self.active_relays and self.active_relays[stream_id].status in ("running", "listening"):
            return self.active_relays[stream_id].to_dict()

        relay = LiveRelayStatus(
            id=stream_id,
            name=item.get("name", "Live Stream"),
            url=item.get("url", ""),
            port=int(item.get("port", 1913)),
            codec=item.get("codec", "h264_nvenc"),
            status="listening",
            error=None,
        )
        self.active_relays[stream_id] = relay
        relay.restart_task = asyncio.create_task(self._auto_restart_loop(relay))
        logger.info(f"Started live relay loop for '{relay.name}' on HTTP port :{relay.port}")
        return relay.to_dict()

    async def stop_stream(self, stream_id: str) -> dict:
        """Stop a running live relay stream cleanly."""
        if stream_id not in self.active_relays:
            return {"id": stream_id, "status": "stopped"}

        relay = self.active_relays[stream_id]
        relay.status = "stopped"

        if relay.restart_task and not relay.restart_task.done():
            relay.restart_task.cancel()
        if relay.log_task and not relay.log_task.done():
            relay.log_task.cancel()

        if relay.process and relay.process.returncode is None:
            try:
                relay.process.terminate()
                await asyncio.sleep(0.5)
                if relay.process.returncode is None:
                    relay.process.kill()
            except Exception as e:
                logger.error(f"Error terminating relay process {stream_id}: {e}")

        relay.process = None
        logger.info(f"Stopped live relay '{relay.name}'")
        return relay.to_dict()

    async def _auto_restart_loop(self, relay: LiveRelayStatus):
        """Loop keeping the FFmpeg listen process running while status is active."""
        warned_nvenc = False
        while relay.status in ("running", "listening"):
            try:
                codec_to_use = relay.codec
                from app.ffmpeg_setup import get_best_encoder
                best_encoder = get_best_encoder()

                # If the user selected hardware transcoding, resolve it to the best available encoder (NVENC -> QSV -> libx264)
                if codec_to_use in ("h264_nvenc", "h264_qsv"):
                    resolved_codec = best_encoder
                else:
                    resolved_codec = codec_to_use

                if resolved_codec != codec_to_use and not warned_nvenc:
                    logger.warning(f"Requested codec '{codec_to_use}' not available on this machine. Falling back to '{resolved_codec}' for {relay.name}")
                    warned_nvenc = True

                codec_to_use = resolved_codec

                cmd = [get_ffmpeg_path()]

                # Network buffering and protocol options
                is_network_input = any(relay.url.startswith(proto) for proto in ("http://", "https://", "rtsp://", "rtmp://", "udp://"))
                if is_network_input:
                    cmd.extend([
                        "-probesize", "10M",
                        "-analyzeduration", "10M"
                    ])

                # RTSP/UDP specific buffer size option and RTSP UDP transport configuration
                if relay.url.startswith("rtsp://") or relay.url.startswith("udp://"):
                    cmd.extend(["-buffer_size", "10M"])
                if relay.url.startswith("rtsp://"):
                    cmd.extend(["-rtsp_transport", "udp"])

                # Reconnect flags for HTTP input streams
                if relay.url.startswith("http://") or relay.url.startswith("https://"):
                    cmd.extend([
                        "-reconnect", "1",
                        "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5",
                    ])
                elif not is_network_input:
                    # Local file input
                    cmd.append("-re")

                cmd.extend(["-i", relay.url])

                if codec_to_use == "h264_nvenc":
                    cmd.extend(["-c:v", "h264_nvenc", "-preset", "p2", "-tune", "ll"])
                elif codec_to_use == "h264_qsv":
                    cmd.extend(["-c:v", "h264_qsv", "-preset", "veryfast"])
                elif codec_to_use == "libx264":
                    cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"])
                else:
                    cmd.extend(["-c:v", "copy"])

                # Output parameters - we set send_buffer_size=10MB on the HTTP socket listener
                # to buffer network traffic for smooth player playback
                cmd.extend([
                    "-c:a", "copy",
                    "-f", "mpegts",
                    "-listen", "1",
                    f"http://0.0.0.0:{relay.port}/?send_buffer_size=10485760"
                ])

                relay.status = "listening"
                relay.error = None

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                relay.process = process

                if relay.log_task and not relay.log_task.done():
                    relay.log_task.cancel()
                relay.log_task = asyncio.create_task(self._read_relay_logs(relay, process.stderr))

                await process.wait()

                # If process exited but status is still active
                if relay.status in ("running", "listening"):
                    if relay.status == "running":
                        # Normal client disconnect (e.g. VLC or watchdog stopped reading).
                        # Reset status back to listening and start a fresh FFmpeg process.
                        relay.status = "listening"
                        await asyncio.sleep(1.0)
                    else:
                        # Process exited before ever reaching 'running' state. Treat as a startup/connection failure.
                        if process.returncode != 0:
                            # Give a tiny slice for log_task to catch any final lines
                            await asyncio.sleep(0.2)
                            logger.error(f"Live relay '{relay.name}' process exited with error code {process.returncode}")
                            
                            # Inspect last logs for error reason
                            error_detail = "Check input stream URL or network connection."
                            if relay.last_logs:
                                # Filter for lines with error keywords to find the root cause
                                keywords = ["error", "refused", "invalid", "timeout", "not found", "cannot", "failed", "unable", "denied"]
                                important_lines = [line for line in relay.last_logs if any(kw in line.lower() for kw in keywords)]
                                if important_lines:
                                    error_detail = important_lines[-1]
                                else:
                                    error_detail = relay.last_logs[-1]

                            relay.status = "error"
                            relay.error = f"FFmpeg error ({process.returncode}): {error_detail}"
                            break
                        else:
                            await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Relay loop error for {relay.name}: {e}")
                relay.status = "error"
                relay.error = str(e)
                break

    async def _read_relay_logs(self, relay: LiveRelayStatus, stderr):
        """Read stderr from FFmpeg relay to update fps, bitrate, and rolling logs."""
        fps_pattern = re.compile(r"fps=\s*([\d\.]+)")
        bitrate_pattern = re.compile(r"bitrate=\s*([\w\./]+)")
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                # Add to rolling log buffer
                relay.last_logs.append(line_str)
                if len(relay.last_logs) > 10:
                    relay.last_logs.pop(0)

                if "frame=" in line_str or "fps=" in line_str:
                    relay.status = "running"
                fps_match = fps_pattern.search(line_str)
                if fps_match:
                    try:
                        relay.fps = float(fps_match.group(1))
                    except ValueError:
                        pass
                br_match = bitrate_pattern.search(line_str)
                if br_match:
                    relay.bitrate = br_match.group(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Log read error for relay {relay.name}: {e}")


# Singleton instance
live_relay_manager = LiveStreamManager()
