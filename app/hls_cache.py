import asyncio
import logging
import time
from typing import Dict, Tuple, Optional
import httpx
from aiohttp import web

# Silence noisy httpx INFO logs (it logs every single 200 OK by default)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

class HlsCacheManager:
    def __init__(self, max_size: int = 300, max_bytes: int = 512 * 1024 * 1024, m3u8_ttl: float = 1.0):
        self.max_size = max_size
        self.max_bytes = max_bytes  # Default 512 MB RAM ceiling
        self.m3u8_ttl = m3u8_ttl
        self._cache: Dict[str, Tuple[float, bytes, str]] = {}
        self._total_bytes: int = 0
        self._locks: Dict[str, asyncio.Event] = {}
        self.client: Optional[httpx.AsyncClient] = None
        self._runners: Dict[int, web.AppRunner] = {}
        self._active_clients: Dict[int, Dict[str, float]] = {}  # port -> {client_ip: last_active_time}

    def get_active_viewer_count(self, public_port: int, window_sec: float = 10.0) -> int:
        clients = self._active_clients.get(public_port, {})
        now = time.time()
        active = [ip for ip, t in clients.items() if now - t <= window_sec]
        # Clean up stale entries periodically
        if len(clients) > 100:
            self._active_clients[public_port] = {ip: t for ip, t in clients.items() if now - t <= 60.0}
        return len(active)

    def start_client(self):
        if not self.client:
            # Enable follow_redirects=True to handle MediaMTX 302 redirects
            self.client = httpx.AsyncClient(timeout=5.0, follow_redirects=True)

    def _evict_if_needed(self, new_bytes: int = 0):
        while self._cache and (len(self._cache) >= self.max_size or (self._total_bytes + new_bytes) > self.max_bytes):
            oldest_key = next(iter(self._cache))
            popped = self._cache.pop(oldest_key, None)
            if popped:
                self._total_bytes = max(0, self._total_bytes - len(popped[1]))

    async def get_file(self, base_url: str, filename_with_qs: str) -> Optional[Tuple[bytes, str]]:
        if not self.client:
            self.start_client()

        # Filename might contain ?session=... so check if the base path is m3u8
        base_path = filename_with_qs.split("?")[0]
        is_m3u8 = base_path.endswith(".m3u8")
        cache_key = f"{base_url.rstrip('/')}/{filename_with_qs}"

        if cache_key in self._cache:
            timestamp, data, media_type = self._cache[cache_key]
            if is_m3u8:
                ttl = 3600.0 if base_path.endswith("index.m3u8") else self.m3u8_ttl
                if time.time() - timestamp <= ttl:
                    return data, media_type
            else:
                self._cache[cache_key] = self._cache.pop(cache_key)
                return data, media_type

        event = self._locks.get(cache_key)
        if event:
            await event.wait()
            if cache_key in self._cache:
                timestamp, data, media_type = self._cache[cache_key]
                if is_m3u8:
                    ttl = 3600.0 if base_path.endswith("index.m3u8") else self.m3u8_ttl
                    if time.time() - timestamp <= ttl:
                        return data, media_type
                else:
                    self._cache[cache_key] = self._cache.pop(cache_key)
                    return data, media_type

        event = asyncio.Event()
        self._locks[cache_key] = event

        try:
            response = await self.client.get(cache_key, timeout=5.0)
            if response.status_code == 200:
                data = response.content
                media_type = response.headers.get("content-type", "application/octet-stream")
                self._evict_if_needed(len(data))
                if cache_key in self._cache:
                    self._total_bytes = max(0, self._total_bytes - len(self._cache[cache_key][1]))
                self._cache[cache_key] = (time.time(), data, media_type)
                self._total_bytes += len(data)
                return data, media_type
            elif response.status_code == 401 and "main_stream.m3u8" in filename_with_qs:
                # Session is dead! Auto-recover by fetching index.m3u8 to get the valid session ID
                idx_resp = await self.client.get(f"{base_url.rstrip('/')}/stream/index.m3u8", timeout=5.0)
                if idx_resp.status_code == 200:
                    import re
                    match = re.search(r'main_stream\.m3u8\?session=([a-zA-Z0-9\-]+)', idx_resp.text)
                    if match:
                        new_session = match.group(1)
                        new_cache_key = f"{base_url.rstrip('/')}/stream/main_stream.m3u8?session={new_session}"
                        new_resp = await self.client.get(new_cache_key, timeout=5.0)
                        if new_resp.status_code == 200:
                            data = new_resp.content
                            media_type = new_resp.headers.get("content-type", "application/octet-stream")
                            self._evict_if_needed(len(data))
                            # Cache the successful NEW data under the OLD cache key
                            # This completely hides the session reset from the stale video player!
                            if cache_key in self._cache:
                                self._total_bytes = max(0, self._total_bytes - len(self._cache[cache_key][1]))
                            self._cache[cache_key] = (time.time(), data, media_type)
                            self._total_bytes += len(data)
                            return data, media_type
                return None
            else:
                # 401 Unauthorized is expected if a client holds a stale session ID.
                # 404 Not Found is expected for internal MediaMTX redirects like ?cookieCheck=1
                if response.status_code not in (401, 404):
                    logger.warning(f"Failed to fetch {cache_key} from upstream: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Error fetching {cache_key}: {e}")
            return None
        finally:
            event.set()
            self._locks.pop(cache_key, None)

    async def start_proxy_server(self, public_port: int, internal_mediamtx_port: int):
        """Starts an aiohttp web server on public_port that proxies to MediaMTX on internal_mediamtx_port."""
        if public_port in self._runners:
            return # Already running

        base_url = f"http://127.0.0.1:{internal_mediamtx_port}"

        async def handle_request(request: web.Request):
            client_ip = request.remote or "127.0.0.1"
            if public_port not in self._active_clients:
                self._active_clients[public_port] = {}
            self._active_clients[public_port][client_ip] = time.time()

            path = request.path_qs.lstrip("/")
            
            # Make the stream available directly at the root URL (e.g. http://127.0.0.1:1923)
            if path == "" or path.startswith("?"):
                result = await self.get_file(base_url, "stream/index.m3u8")
                if not result:
                    return web.Response(status=404, text="File not found")
                data, media_type = result
                
                # Rewrite relative URLs in the master playlist to absolute URLs
                # This bypasses VLC bugs where parsing naked domains (no trailing slash) breaks relative URL resolution
                data_str = data.decode("utf-8", errors="replace")
                host_url = f"{request.scheme}://{request.host}"
                rewritten_lines = []
                for line in data_str.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if not line.startswith("http") and not line.startswith("/"):
                            line = f"{host_url}/stream/{line}"
                    rewritten_lines.append(line)
                
                return web.Response(body="\n".join(rewritten_lines).encode("utf-8"), content_type=media_type)

            # For all other paths (which will now correctly include 'stream/' thanks to the rewrite above)
            result = await self.get_file(base_url, path)
            if not result:
                return web.Response(status=404, text="File not found")
            data, media_type = result
            return web.Response(body=data, content_type=media_type)

        app = web.Application()
        # Route all paths to the proxy handler
        app.router.add_get("/{path:.*}", handle_request)
        
        # Disable aiohttp access log spam since HLS generates 10s of requests per second
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', public_port)
        await site.start()
        self._runners[public_port] = runner
        logger.info(f"Started HLS Native Python Proxy on port {public_port} -> MediaMTX port {internal_mediamtx_port}")

    async def stop_proxy_server(self, public_port: int):
        """Stops the proxy server on public_port."""
        self._active_clients.pop(public_port, None)
        runner = self._runners.pop(public_port, None)
        if runner:
            await runner.cleanup()
            logger.info(f"Stopped HLS Native Python Proxy on port {public_port}")

# Global singleton
hls_cache = HlsCacheManager(max_size=300)
