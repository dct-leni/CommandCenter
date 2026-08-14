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

# Enforce 1ms Windows OS system timer interrupt resolution to eliminate 15.6ms quantum slips in GDIGrab
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

# Per-relay asyncio Queue used to pipe PCM data from the Firefox extension WebSocket into FFmpeg stdin
_TAB_AUDIO_QUEUES: Dict[str, asyncio.Queue] = {}


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
    audio_ws_task: Optional[asyncio.Task] = field(default=None, repr=False)

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

            use_vpn = item.get("use_vpn")
            if use_vpn is None:
                use_vpn = item.get("vpn_mode", "none") != "none"

            global_vpn = getattr(cfg.streamer, "global_vpn", {}) or {}
            global_vpn_mode = global_vpn.get("mode", "none")

            if sid in self.active_relays:
                relay = self.active_relays[sid]
                relay.name = item.get("name", relay.name)
                relay.url = item.get("url", relay.url)
                relay.port = item.get("port", relay.port)
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

            url_str = item.get("url", "").lower()
            stype = item.get("stream_type", "http")
            if stype != "web":
                if item.get("web_url") or any(kw in url_str for kw in ("wtfismyip", "netflix", "youtube.com/watch")):
                    stype = "web"

            d["use_vpn"] = bool(use_vpn)
            d["global_vpn_mode"] = global_vpn_mode
            d["stream_type"] = stype
            results.append(d)
        return results

    def trigger_thumbnail_generation(self, stream_id: str, port: int):
        """Trigger background generation of live stream thumbnail snapshot from local stream port (every 10min)."""
        now = time.time()
        last_time = _LAST_THUMBNAIL_TIME.get(stream_id, 0.0)
        thumb_path = THUMBNAILS_DIR / f"live_{stream_id}.jpg"
        if thumb_path.exists() and (now - last_time < 600.0):
            return

        _LAST_THUMBNAIL_TIME[stream_id] = now
        
        async def task():
            try:
                THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
                temp_path = THUMBNAILS_DIR / f"live_{stream_id}_temp.jpg"

                # Capture 1 frame directly from local HTTP stream output
                cmd = [
                    get_ffmpeg_path(),
                    "-ss", "0",
                    "-i", f"http://127.0.0.1:{port}/",
                    "-vframes", "1",
                    "-q:v", "6",
                    "-y",
                    str(temp_path)
                ]
                
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                    if temp_path.exists():
                        if thumb_path.exists():
                            thumb_path.unlink()
                        temp_path.rename(thumb_path)
                except Exception:
                    pass
                finally:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass
            except Exception:
                pass

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

        url_str = item.get("url", "").lower()
        is_web = item.get("stream_type") == "web" or bool(item.get("web_url")) or any(kw in url_str for kw in ("wtfismyip", "netflix", "youtube.com/watch"))
        current_relay = self.active_relays.get(stream_id)

        # Stage 1 for Web Streams: If browser isn't opened yet (or status is stopped), open default browser first!
        if is_web:
            if not current_relay or current_relay.status not in ("browser_ready", "running", "listening"):
                return await self.start_browser_for_stream(stream_id)

        if stream_id in self.active_relays and self.active_relays[stream_id].status in ("running", "listening"):
            return self.active_relays[stream_id].to_dict()

        if current_relay:
            relay = current_relay
            relay.status = "listening"
            relay.error = None
        else:
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
                        # Read up to 64KB socket payload chunks so full H.264 video frames (40-80KB)
                        # stream instantly in 1-2 reads without 30-fragment event loop delays
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        
                        if relay.clients:
                            for q in list(relay.clients.keys()):
                                try:
                                    q.put_nowait(chunk)
                                except asyncio.QueueFull:
                                    # Client fell behind — drain all stale data so it jumps to live
                                    while not q.empty():
                                        try:
                                            q.get_nowait()
                                        except asyncio.QueueEmpty:
                                            break
                                    try:
                                        q.put_nowait(chunk)
                                    except Exception:
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

            # Enable TCP_NODELAY on the client connection
            try:
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    import socket
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

            # Low-latency queue size (128 chunks) prevents burst packet dumps that cause playback stacking
            queue = asyncio.Queue(maxsize=128)
            relay.clients[queue] = writer

            async def client_write_loop():
                try:
                    while True:
                        chunk = await queue.get()
                        writer.write(chunk)
                        queue.task_done()

                        # Batch-write any remaining queued chunks
                        while not queue.empty():
                            try:
                                next_chunk = queue.get_nowait()
                                writer.write(next_chunk)
                                queue.task_done()
                            except asyncio.QueueEmpty:
                                break

                        try:
                            await writer.drain()
                        except Exception:
                            break
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
                # Wait until client socket is closed or write_task finishes
                while not write_task.done():
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
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
            from app.web_stream import web_stream_manager
            await asyncio.to_thread(web_stream_manager.close_browser, stream_id)
            return {"id": stream_id, "status": "stopped"}

        relay = self.active_relays[stream_id]
        relay.status = "stopped"

        if relay.restart_task and not relay.restart_task.done():
            relay.restart_task.cancel()
            try:
                await relay.restart_task
            except asyncio.CancelledError:
                pass
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
                await asyncio.sleep(0.1)
                if relay.process.returncode is None:
                    relay.process.kill()
            except Exception as e:
                logger.error(f"Error terminating relay process {stream_id}: {e}")

        relay.process = None
        from app.audio_router import stop_process_audio_capture
        stop_process_audio_capture(stream_id)
        from app.vpn_manager import vpn_manager
        vpn_manager.stop_vpn_for_stream(stream_id)
        from app.web_stream import web_stream_manager
        await asyncio.to_thread(web_stream_manager.close_browser, stream_id)

        # Restore user's default audio device if it was routed to VB-Cable for this stream
        if stream_id in self.active_relays:
            _relay_item = load_config()
            _item = next((x for x in _relay_item.streamer.live_streams if x.get("id") == stream_id), {})
            if _item.get("stream_type") == "web":
                pass  # Audio routing via Firefox cubeb pref — nothing to restore

        # Cancel the tab audio WebSocket server if running
        if relay.audio_ws_task and not relay.audio_ws_task.done():
            relay.audio_ws_task.cancel()
            try:
                await relay.audio_ws_task
            except asyncio.CancelledError:
                pass
            relay.audio_ws_task = None
        # Remove the audio queue so no dangling data is held
        _TAB_AUDIO_QUEUES.pop(stream_id, None)

        logger.info(f"Stopped live relay '{relay.name}'")
        return relay.to_dict()

    async def start_browser_for_stream(self, stream_id: str) -> dict:
        """Launch browser instance for a web stream."""
        cfg = load_config()
        stream_item = next((x for x in cfg.streamer.live_streams if x.get("id") == stream_id), {})
        if not stream_item:
            raise ValueError(f"Stream {stream_id} not found")

        from app.vpn_manager import vpn_manager
        proxy_url = vpn_manager.get_proxy_url_for_stream(stream_item)
        if proxy_url:
            await asyncio.sleep(0.5)  # Wait 500ms for WireGuard local proxy socket readiness

        from app.web_stream import web_stream_manager
        await asyncio.to_thread(web_stream_manager.close_browser, stream_id)

        name = stream_item.get("name", "Web Stream")
        target_url = stream_item.get("url", "")
        
        window_title = await asyncio.to_thread(web_stream_manager.launch_browser, stream_id, name, target_url, proxy_url)
        # Capture actual Firefox window HWND so close_browser can kill by window
        await asyncio.to_thread(web_stream_manager.wait_for_window_title, stream_id, name, target_url, 10.0)

        relay = self.active_relays.get(stream_id)
        if not relay:
            relay = LiveRelayStatus(
                id=stream_id,
                name=name,
                url=target_url,
                port=int(stream_item.get("port", 1916)),
                status="browser_ready",
                error=None,
            )
            self.active_relays[stream_id] = relay
        else:
            relay.status = "browser_ready"
            relay.error = None

        logger.info(f"Opened browser for web stream '{name}' ({stream_id}). Status: browser_ready.")

        return relay.to_dict()

    async def _auto_restart_loop(self, relay: LiveRelayStatus):
        """Loop keeping the FFmpeg listen process running while status is active."""
        from app.vpn_manager import vpn_manager
        cfg = load_config()
        stream_item = next((x for x in cfg.streamer.live_streams if x.get("id") == relay.id), {})
        proxy_url = vpn_manager.get_proxy_url_for_stream(stream_item)
        is_web = stream_item.get("stream_type") == "web"
        from app.ffmpeg_setup import probe_source_codec, get_relay_params, get_best_encoder, get_relay_encoding_params, format_ffmpeg_headers
        if is_web:
            from app.ffmpeg_setup import get_screen_capture_params
            encoder = get_best_encoder()
            video_params = get_screen_capture_params(encoder)
            logger.info(f"Web stream capture for '{relay.name}' — encoding with {encoder} (screen-capture profile)")
            # Audio routes via Firefox cubeb pref in user.js (set at profile creation time).
        else:
            logger.info(f"Probing source codec for '{relay.name}' at {relay.url} (proxy: {proxy_url or 'none'}) …")
            source_codec = await asyncio.get_event_loop().run_in_executor(
                None, probe_source_codec, relay.url, 8, proxy_url
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

                target_pid = None
                if is_web:
                    from app.web_stream import web_stream_manager
                    hwnd = await asyncio.get_event_loop().run_in_executor(
                        None, web_stream_manager.get_window_hwnd, relay.id, relay.name, relay.url
                    )

                    if hwnd:
                        input_target = f"hwnd=0x{hwnd:x}"
                        logger.info(f"Web stream '{relay.name}' GDIGrab targeting HWND: {input_target}")
                        if os.name == "nt":
                            try:
                                import ctypes
                                from ctypes import wintypes
                                _dw_pid = wintypes.DWORD()
                                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(_dw_pid))
                                if _dw_pid.value:
                                    target_pid = _dw_pid.value
                            except Exception:
                                pass
                    else:
                        raise RuntimeError(f"Firefox browser window not found for web stream '{relay.name}'. Please click 'Open Browser' first.")

                    if not target_pid:
                        browser_proc = web_stream_manager.browser_processes.get(relay.id)
                        if browser_proc and browser_proc.pid:
                            target_pid = browser_proc.pid

                    logger.info(f"Web stream '{relay.name}': GDIGrab targeting '{input_target}' with PID={target_pid} (Native Process Loopback)")

                    # Restore window if minimized
                    if hwnd:
                        try:
                            import ctypes
                            _user32 = ctypes.windll.user32
                            if _user32.IsIconic(hwnd):
                                _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                                await asyncio.sleep(0.3)
                        except Exception:
                            pass

                    cmd.extend([
                        # Video: GDIGrab browser window HWND capture at 30 FPS (Master Clock Input 0)
                        "-use_wallclock_as_timestamps", "1",
                        "-thread_queue_size", "1024",
                        "-f", "gdigrab",
                        "-framerate", "30",
                        "-draw_mouse", "0",
                        "-i", input_target,
                        # Audio: Native PROCESS_LOOPBACK raw s16le PCM via pipe:0 (Input 1)
                        "-thread_queue_size", "1024",
                        "-f", "s16le",
                        "-ac", "2",
                        "-ar", "48000",
                        "-i", "pipe:0",
                        "-map", "0:v:0",
                        "-map", "1:a:0",
                        "-vf", "crop=iw:ih-38:0:38,format=yuv420p",
                    ])
                else:
                    if proxy_url:
                        if proxy_url.startswith("socks5://") or proxy_url.startswith("socks4://"):
                            cmd.extend(["-socks_proxy", proxy_url])
                        else:
                            cmd.extend(["-http_proxy", proxy_url])

                    formatted_headers = format_ffmpeg_headers(relay.url)
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
                from app.ffmpeg_setup import get_audio_params
                cmd.extend(get_audio_params(is_web))
                
                # -bsf:v dump_extra is ONLY needed for stream copy (-c:v copy). NVENC already generates Annex B SPS/PPS NAL units natively.
                if not is_web:
                    cmd.extend(["-bsf:v", "dump_extra"])

                interleave_delta = "0" if is_web else "50000"
                cmd.extend([
                    "-avoid_negative_ts", "make_zero",
                    "-fflags", "+genpts",
                    "-max_interleave_delta", interleave_delta, # 0 for web streams forces instant video packet output without interleave holds
                    "-flush_packets", "1",        # Flush MPEG-TS packets immediately
                    "-f", "mpegts",
                    f"tcp://127.0.0.1:{relay.loopback_port}?tcp_nodelay=1"
                ])

                relay.status = "listening"
                relay.error = None

                env = os.environ.copy()
                if proxy_url:
                    env["http_proxy"] = proxy_url
                    env["https_proxy"] = proxy_url

                # Web streams use PROCESS_LOOPBACK raw PCM over native OS pipe:0
                r_fd, w_fd = None, None
                if is_web:
                    r_fd, w_fd = os.pipe()

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=r_fd if is_web else subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,  # 1 MB — prevents LimitOverrunError on long FFmpeg lines
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    env=env,
                )
                if is_web and r_fd is not None:
                    try:
                        os.close(r_fd)
                    except Exception:
                        pass

                relay.process = process

                if is_web and w_fd is not None:
                    from app.audio_router import start_process_audio_capture
                    start_process_audio_capture(relay.id, target_pid or 0, w_fd)

                if relay.log_task and not relay.log_task.done():
                    relay.log_task.cancel()
                relay.log_task = asyncio.create_task(self._read_relay_logs(relay, process.stderr))

                # Background task for 10-minute live snapshot generation from local HTTP stream output
                async def _periodic_thumb_task(sid: str, s_port: int):
                    await asyncio.sleep(3.0)
                    while relay.status in ("running", "listening"):
                        try:
                            self.trigger_thumbnail_generation(sid, s_port)
                        except Exception:
                            pass
                        await asyncio.sleep(600.0)

                thumb_loop_task = asyncio.create_task(_periodic_thumb_task(relay.id, relay.port))

                await process.wait()
                if is_web:
                    from app.audio_router import stop_process_audio_capture
                    stop_process_audio_capture(relay.id)
                if thumb_loop_task and not thumb_loop_task.done():
                    thumb_loop_task.cancel()

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
                        if is_web:
                            from app.web_stream import web_stream_manager
                            asyncio.create_task(asyncio.to_thread(web_stream_manager.close_browser, relay.id))
                        break
                    else:
                        await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                if is_web:
                    from app.audio_router import stop_process_audio_capture
                    stop_process_audio_capture(relay.id)
                break
            except Exception as e:
                if is_web:
                    from app.audio_router import stop_process_audio_capture
                    stop_process_audio_capture(relay.id)
                logger.error(f"Relay loop error for {relay.name}: {e}")
                relay.status = "error"
                relay.error = str(e)
                if is_web:
                    from app.web_stream import web_stream_manager
                    asyncio.create_task(asyncio.to_thread(web_stream_manager.close_browser, relay.id))
                break

    async def _read_relay_logs(self, relay: LiveRelayStatus, stderr):
        """Read stderr from FFmpeg relay to update fps, bitrate, and rolling logs."""
        fps_pattern = re.compile(r"fps=\s*([\d\.]+)")
        bitrate_pattern = re.compile(r"bitrate=\s*([\w\./]+)")
        try:
            while True:
                try:
                    line = await stderr.readline()
                except (asyncio.LimitOverrunError, ValueError):
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
