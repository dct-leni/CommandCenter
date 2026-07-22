"""
Live Stream Relay Manager for CommandCenter.
Manages background FFmpeg processes to ingest HTTP/RTSP/RTMP streams,
encode them (with optional NVENC hardware acceleration), and broadcast
MPEG-TS over HTTP to multiple concurrent client connections.
Uses a local TCP loopback to avoid stdout binary corruption on Windows.
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
    status: str = "stopped"  # stopped, running, listening, error
    error: Optional[str] = None
    fps: float = 0.0
    bitrate: str = "0kbits/s"
    process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    restart_task: Optional[asyncio.Task] = field(default=None, repr=False)
    log_task: Optional[asyncio.Task] = field(default=None, repr=False)
    last_logs: List[str] = field(default_factory=list, repr=False)
    clients: dict = field(default_factory=dict, repr=False)
    server: Optional[asyncio.Server] = field(default=None, repr=False)
    loopback_server: Optional[asyncio.Server] = field(default=None, repr=False)
    loopback_port: int = 0

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
        
        # UI status: show "running" if we have active clients connected, otherwise "listening"
        status_to_show = self.status
        if self.status in ("running", "listening"):
            status_to_show = "running" if self.clients else "listening"

        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "port": self.port,
            "codec": best_encoder,
            "status": status_to_show,
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

            vpn_mode = item.get("vpn_mode", "none")
            vpn_profile_name = item.get("vpn_profile_name", "")
            vpn_profile_content = item.get("vpn_profile_content", "")
            proxy_url = item.get("proxy_url", "")
            headers = item.get("headers", "")

            if sid in self.active_relays:
                relay = self.active_relays[sid]
                # Only capture thumbnails when viewers are actively watching
                if relay.status == "running":
                    self.trigger_thumbnail_generation(sid, f"http://127.0.0.1:{relay.port}/")
                d = relay.to_dict()
            else:
                thumb_path = THUMBNAILS_DIR / f"live_{sid}.jpg"
                has_thumb = thumb_path.exists()
                mtime = int(thumb_path.stat().st_mtime) if has_thumb else 0
                from app.ffmpeg_setup import get_best_encoder
                best_encoder = get_best_encoder()
                d = {
                    "id": sid,
                    "name": item.get("name", "Unnamed Stream"),
                    "url": item.get("url", ""),
                    "port": item.get("port", 1913),
                    "codec": best_encoder,
                    "status": "stopped",
                    "error": None,
                    "fps": 0.0,
                    "bitrate": "0kbits/s",
                    "has_thumbnail": has_thumb,
                    "thumbnail_url": f"/api/streamer/live_stream/{sid}/thumbnail?v={mtime}",
                }

            d["vpn_mode"] = vpn_mode
            d["vpn_profile_name"] = vpn_profile_name
            d["vpn_profile_content"] = vpn_profile_content
            d["proxy_url"] = proxy_url
            d["headers"] = headers
            results.append(d)
        return results

    def trigger_thumbnail_generation(self, stream_id: str, stream_url: str):
        """Trigger background generation of live stream thumbnail if running."""
        now = time.time()
        last_time = _LAST_THUMBNAIL_TIME.get(stream_id, 0.0)
        if now - last_time < 60.0:  # rate limit to once per 60 seconds (1 minute)
            return

        _LAST_THUMBNAIL_TIME[stream_id] = now
        
        async def task():
            try:
                THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
                thumb_path = THUMBNAILS_DIR / f"live_{stream_id}.jpg"
                temp_path = THUMBNAILS_DIR / f"live_{stream_id}_temp.jpg"

                cmd = [
                    get_ffmpeg_path(),
                    "-skip_frame", "nokey",
                    "-i", stream_url,
                    "-vframes", "1",
                    "-q:v", "6",
                    "-update", "1",
                    "-y",
                    str(temp_path)
                ]
                
                # Start process and wait with a short timeout
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=15.0)
                    if proc.returncode != 0:
                        logger.warning(f"FFmpeg thumbnail generation failed (code {proc.returncode}) for live stream {stream_id}")
                    elif temp_path.exists():
                        if thumb_path.exists():
                            thumb_path.unlink()
                        temp_path.rename(thumb_path)
                except asyncio.TimeoutError:
                    logger.warning(f"FFmpeg thumbnail generation timed out (15s) for live stream {stream_id}")
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
            status="listening",
            error=None,
        )
        self.active_relays[stream_id] = relay

        # Start Loopback Server to receive binary data from FFmpeg
        try:
            async def handle_loopback(reader, writer):
                relay.status = "running"
                try:
                    while relay.status in ("running", "listening"):
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        
                        if relay.clients:
                            for q in list(relay.clients.keys()):
                                try:
                                    q.put_nowait(chunk)
                                except asyncio.QueueFull:
                                    # Client is too slow, drop chunk to avoid memory build-up
                                    pass
                except Exception as e:
                    logger.error(f"Loopback error for {relay.name}: {e}")
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            relay.loopback_server = await asyncio.start_server(handle_loopback, "127.0.0.1", 0)
            relay.loopback_port = relay.loopback_server.sockets[0].getsockname()[1]
        except Exception as e:
            logger.error(f"Failed to start loopback server for {relay.name}: {e}")
            relay.status = "error"
            relay.error = f"Loopback init error: {e}"
            return relay.to_dict()

        # Handle incoming HTTP clients on the TCP socket
        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                # Read HTTP request headers to satisfy client handshake
                await reader.readuntil(b"\r\n\r\n")
            except Exception:
                try:
                    writer.close()
                except Exception:
                    pass
                return

            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: video/mp2t\r\n"
                "Connection: close\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "\r\n"
            )
            try:
                writer.write(headers.encode("utf-8"))
                await writer.drain()
            except Exception:
                try:
                    writer.close()
                except Exception:
                    pass
                return

            # Bounded queue to prevent memory leaks if client blocks
            queue = asyncio.Queue(maxsize=100)
            relay.clients[queue] = writer

            async def client_write_loop():
                try:
                    while True:
                        chunk = await queue.get()
                        writer.write(chunk)
                        await writer.drain()
                        queue.task_done()
                except Exception:
                    pass
                finally:
                    relay.clients.pop(queue, None)
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

            # Start the background writing loop
            write_task = asyncio.create_task(client_write_loop())
            
            try:
                # Keep socket alive until client disconnects (read returns EOF)
                await reader.read()
            except Exception:
                pass
            finally:
                write_task.cancel()
                try:
                    await write_task
                except Exception:
                    pass

        # Start the Python TCP Server to broadcast stream packets
        try:
            relay.server = await asyncio.start_server(handle_client, "0.0.0.0", relay.port)
        except Exception as e:
            logger.error(f"Failed to start TCP listener on port {relay.port}: {e}")
            # Clean up loopback server
            if relay.loopback_server:
                relay.loopback_server.close()
                relay.loopback_server = None
            relay.status = "error"
            relay.error = f"Port bind error: {e}"
            return relay.to_dict()

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

        # Stop TCP server
        if relay.server:
            try:
                relay.server.close()
            except Exception as e:
                logger.error(f"Error closing relay TCP server: {e}")
            relay.server = None

        # Stop Loopback server
        if relay.loopback_server:
            try:
                relay.loopback_server.close()
            except Exception as e:
                logger.error(f"Error closing loopback server: {e}")
            relay.loopback_server = None

        # Disconnect all connected clients
        for writer in list(relay.clients.values()):
            try:
                writer.close()
            except Exception:
                pass
        relay.clients.clear()

        # Kill FFmpeg process
        if relay.process and relay.process.returncode is None:
            try:
                relay.process.terminate()
                await asyncio.sleep(0.5)
                if relay.process.returncode is None:
                    relay.process.kill()
            except Exception as e:
                logger.error(f"Error terminating relay process {stream_id}: {e}")

        relay.process = None
        from app.vpn_manager import vpn_manager
        vpn_manager.stop_vpn_for_stream(stream_id)
        logger.info(f"Stopped live relay '{relay.name}'")
        return relay.to_dict()

    async def _auto_restart_loop(self, relay: LiveRelayStatus):
        """Loop keeping the FFmpeg listen process running while status is active."""
        from app.vpn_manager import vpn_manager
        cfg = load_config()
        stream_item = next((x for x in cfg.streamer.live_streams if x.get("id") == relay.id), {})
        proxy_url = vpn_manager.start_vpn_for_stream(relay.id, stream_item)
        headers = stream_item.get("headers", "")
        from app.ffmpeg_setup import probe_source_codec, get_relay_params, get_best_encoder, get_relay_encoding_params, format_ffmpeg_headers
        logger.info(f"Probing source codec for '{relay.name}' at {relay.url} (proxy: {proxy_url or 'none'}) …")
        source_codec = await asyncio.get_event_loop().run_in_executor(
            None, probe_source_codec, relay.url, 8, proxy_url, headers
        )
        if source_codec == "h264":
            video_params = get_relay_params()   # stream copy — 0 GPU
            logger.info(f"Source is H.264 — using stream copy for '{relay.name}'")
        else:
            encoder = get_best_encoder()
            video_params = get_relay_encoding_params(encoder)
            logger.info(f"Source codec '{source_codec}' — re-encoding with {encoder} for '{relay.name}'")

        while relay.status in ("running", "listening"):
            try:

                cmd = [get_ffmpeg_path()]

                if proxy_url:
                    if proxy_url.startswith("socks5://") or proxy_url.startswith("socks4://"):
                        cmd.extend(["-socks_proxy", proxy_url])
                    else:
                        cmd.extend(["-http_proxy", proxy_url])

                formatted_headers = format_ffmpeg_headers(headers, relay.url)
                if formatted_headers and (relay.url.startswith("http://") or relay.url.startswith("https://")):
                    cmd.extend(["-headers", formatted_headers])

                # Detect HLS — URL ends with .m3u8 or contains /m3u8
                is_hls = ".m3u8" in relay.url.lower()

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

                if is_hls:
                    cmd.extend([
                        "-allowed_extensions", "ALL",
                        "-allowed_segment_extensions", "ALL",
                        "-extension_picky", "0",
                        "-timeout", "10000000",
                    ])
                elif relay.url.startswith("http://") or relay.url.startswith("https://"):
                    # Plain HTTP MPEG-TS stream
                    cmd.extend([
                        "-reconnect", "1",
                        "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5",
                        "-timeout", "5000000",
                    ])
                elif relay.url.startswith("rtsp://"):
                    cmd.extend(["-stimeout", "5000000"])
                elif relay.url.startswith("rtmp://"):
                    cmd.extend(["-rw_timeout", "5000000"])
                elif not is_network_input:
                    # Local file input
                    cmd.extend(["-re", "-stream_loop", "-1"])

                cmd.extend(["-i", relay.url])

                cmd.extend(video_params)

                # Output parameters - stream to Python's local loopback TCP port
                cmd.extend([
                    "-c:a", "copy",
                    "-bsf:v", "dump_extra",  # Repeat H.264/H.265 SPS/PPS headers before keyframes
                    "-f", "mpegts",
                    f"tcp://127.0.0.1:{relay.loopback_port}"
                ])

                relay.status = "listening"
                relay.error = None

                env = os.environ.copy()
                if proxy_url:
                    env["http_proxy"] = proxy_url
                    env["https_proxy"] = proxy_url

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,  # 1 MB — prevents LimitOverrunError on long FFmpeg lines
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    env=env,
                )
                relay.process = process

                if relay.log_task and not relay.log_task.done():
                    relay.log_task.cancel()
                relay.log_task = asyncio.create_task(self._read_relay_logs(relay, process.stderr))

                await process.wait()

                # If process exited but status is still active (not stopped by user)
                if relay.status in ("running", "listening"):
                    if process.returncode != 0:
                        # Give a tiny slice for log_task to catch any final lines
                        await asyncio.sleep(0.2)
                        logger.error(f"Live relay '{relay.name}' process exited with error code {process.returncode}")
                        
                        # Inspect last logs for error reason
                        error_detail = "Check input stream URL or network connection."
                        if relay.last_logs:
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
                try:
                    line = await stderr.readline()
                except asyncio.LimitOverrunError:
                    # FFmpeg wrote a line longer than the StreamReader buffer.
                    # Drain and discard the oversized chunk, then continue.
                    await stderr.read(1024 * 1024)
                    continue
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                # Add to rolling log buffer
                relay.last_logs.append(line_str)
                if len(relay.last_logs) > 10:
                    relay.last_logs.pop(0)

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
