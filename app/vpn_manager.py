"""
VPN and Proxy Manager for CommandCenter Live Relay Streams.
Manages isolated user-space VPN proxy subprocesses (WireGuard via wireproxy,
or custom SOCKS5/HTTP proxies) so individual live streams can be
ingested over VPN without altering system network gateways or throttling
local client network connections.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from app.config import load_config

logger = logging.getLogger(__name__)

# Base directory for storing temporary runtime config files
BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
TEMP_VPN_DIR = BASE_DIR / "temp"
TEMP_VPN_DIR.mkdir(exist_ok=True)


class LocalProxyBridge:
    """
    High-performance local HTTP-to-SOCKS5/HTTP proxy bridge.
    Accepts unauthenticated HTTP/CONNECT requests from Chrome/FFmpeg on 127.0.0.1:<port>
    and forwards them to a remote authenticated SOCKS5 or HTTP proxy server (e.g. ZoogVPN SOCKS5).
    Optimized with TCP_NODELAY, ThreadPoolExecutor worker recycling, and non-blocking select() multiplexing.
    """
    def __init__(self, local_port: int, remote_host: str, remote_port: int, scheme: str = "socks5", username: str = "", password: str = ""):
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.scheme = scheme.lower() if scheme else "socks5"
        self.username = username
        self.password = password
        self.server_sock = None
        self.running = False
        self._thread = None
        self._executor = None

    def start(self):
        import socket
        import threading
        from concurrent.futures import ThreadPoolExecutor

        self._executor = ThreadPoolExecutor(max_workers=64)
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.server_sock.bind(("127.0.0.1", self.local_port))
        self.server_sock.listen(128)
        self.running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        import time
        time.sleep(0.2)
        logger.info(f"Started high-performance local proxy bridge at http://127.0.0.1:{self.local_port} -> {self.scheme}://{self.remote_host}:{self.remote_port}")

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if self._executor:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass

    def _listen_loop(self):
        import socket
        while self.running:
            try:
                client_sock, _ = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._executor.submit(self._handle_client, client_sock)
            except Exception:
                break

    def _handle_client(self, client_sock):
        import socket
        import socks
        try:
            client_sock.settimeout(5.0)
            req_data = client_sock.recv(16384)
            if not req_data:
                client_sock.close()
                return

            req_str = req_data.decode("utf-8", errors="ignore")
            first_line = req_str.split("\r\n")[0]
            parts = first_line.split(" ")
            if len(parts) < 2:
                client_sock.close()
                return

            method, target = parts[0], parts[1]

            remote_sock = socks.socksocket()
            remote_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            proxy_type = socks.SOCKS5 if "socks" in self.scheme else socks.HTTP
            remote_sock.set_proxy(
                proxy_type,
                self.remote_host,
                self.remote_port,
                username=self.username if self.username else None,
                password=self.password if self.password else None,
                rdns=True,
            )
            remote_sock.settimeout(10.0)

            if method == "CONNECT":
                host_port = target.split(":")
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 443
                
                try:
                    remote_sock.connect((host, port))
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._pipe(client_sock, remote_sock)
                except Exception as ce:
                    logger.debug(f"LocalProxyBridge CONNECT error for {host}:{port}: {ce}")
                    try:
                        client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    except Exception:
                        pass
            else:
                if "://" in target:
                    url_part = target.split("://", 1)[1]
                else:
                    url_part = target
                host_part = url_part.split("/", 1)[0]
                if ":" in host_part:
                    host, port = host_part.split(":")
                    port = int(port)
                else:
                    host = host_part
                    port = 80

                try:
                    remote_sock.connect((host, port))
                    remote_sock.sendall(req_data)
                    self._pipe(client_sock, remote_sock)
                except Exception as ce:
                    logger.debug(f"LocalProxyBridge HTTP error for {host}:{port}: {ce}")
                    try:
                        client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"LocalProxyBridge connection handling error: {e}")
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def _pipe(self, s1, s2):
        import select
        import socket
        import time

        # Extract pure native C OS sockets, bypassing PySocks wrapper logic after connection establishment
        s1_native = getattr(s1, "_sock", s1)
        s2_native = getattr(s2, "_sock", s2)

        s1_native.setblocking(False)
        s2_native.setblocking(False)

        raw_to_sock = {s1_native: (s1_native, s2_native), s2_native: (s2_native, s1_native)}
        sockets = [s1_native, s2_native]
        bufsize = 131072
        idle_start = time.time()

        while self.running:
            try:
                readable, _, errors = select.select(sockets, [], sockets, 1.0)
                if errors:
                    break
                if not readable:
                    if time.time() - idle_start > 15.0:
                        break
                    continue

                idle_start = time.time()
                for r in readable:
                    src, dst = raw_to_sock[r]
                    try:
                        data = src.recv(bufsize)
                        if not data:
                            return
                        dst.sendall(data)
                    except (socket.error, BlockingIOError, InterruptedError):
                        return
                    except Exception:
                        return
            except Exception:
                break

        try:
            s1_native.close()
        except Exception:
            pass
        try:
            s2_native.close()
        except Exception:
            pass


class VPNProcess:
    def __init__(self, stream_id: str, mode: str, proxy_url: str, process: Optional[subprocess.Popen] = None, temp_file: Optional[Path] = None, bridge: Optional[LocalProxyBridge] = None):
        self.stream_id = stream_id
        self.mode = mode
        self.proxy_url = proxy_url
        self.process = process
        self.temp_file = temp_file
        self.bridge = bridge

    def stop(self):
        if self.bridge:
            try:
                self.bridge.stop()
            except Exception:
                pass
            self.bridge = None
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        if self.temp_file and self.temp_file.exists():
            try:
                self.temp_file.unlink()
            except Exception:
                pass


def parse_proxy_parts(raw_url: str, username: str = "", password: str = ""):
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return "", 0, "socks5", "", ""

    from urllib.parse import urlparse, unquote
    if "://" not in raw_url:
        raw_url = f"socks5://{raw_url}"

    parsed = urlparse(raw_url)
    scheme = parsed.scheme or "socks5"
    host = parsed.hostname or ""
    port = parsed.port or (1080 if "socks" in scheme else 8080)

    user = unquote(parsed.username) if parsed.username else (username or "").strip()
    pwd = unquote(parsed.password) if parsed.password else (password or "").strip()

    return host, port, scheme, user, pwd


class VPNManager:
    """Singleton managing global VPN proxy processes."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VPNManager, cls).__new__(cls)
            cls._instance._global_vpn_process: Optional[VPNProcess] = None
            cls._instance._global_proxy_url: Optional[str] = None
            cls._instance._port_counter = 10500
        return cls._instance

    def _allocate_port(self) -> int:
        self._port_counter += 1
        return self._port_counter

    def start_global_vpn(self) -> Optional[str]:
        """
        Start Global VPN (WireGuard or Proxy) if configured in global settings.
        Returns local proxy URL (e.g. 'http://127.0.0.1:10501') or None if disabled.
        """
        self.stop_global_vpn()

        cfg = load_config()
        global_vpn = getattr(cfg.streamer, "global_vpn", {}) or {}
        mode = global_vpn.get("mode", "none")

        if mode == "none" or not mode:
            return None

        if mode == "proxy":
            raw_url = global_vpn.get("proxy_url", "")
            user = global_vpn.get("proxy_username", "")
            pwd = global_vpn.get("proxy_password", "")
            host, port, scheme, user, pwd = parse_proxy_parts(raw_url, user, pwd)

            if not host:
                logger.warning("Global VPN set to proxy mode but proxy_url host is empty.")
                return None

            local_port = self._allocate_port()
            bridge = LocalProxyBridge(local_port, host, port, scheme, user, pwd)
            bridge.start()

            self._global_proxy_url = f"http://127.0.0.1:{local_port}"
            self._global_vpn_process = VPNProcess(stream_id="global", mode="proxy", proxy_url=self._global_proxy_url, bridge=bridge)
            logger.info(f"Global VPN active via local proxy bridge {self._global_proxy_url} -> {scheme}://{host}:{port}")
            return self._global_proxy_url

        if mode == "wireguard":
            content = global_vpn.get("profile_content", "").strip()
            if not content:
                logger.warning("Global VPN set to WireGuard mode but profile content is empty.")
                return None

            proxy_port = self._allocate_port()
            proxy_url = f"http://127.0.0.1:{proxy_port}"

            wireproxy_conf = content
            if "[Socks5]" not in wireproxy_conf and "[HTTP]" not in wireproxy_conf:
                wireproxy_conf += f"\n\n[HTTP]\nBindAddress = 127.0.0.1:{proxy_port}\n"
                wireproxy_conf += f"\n[Socks5]\nBindAddress = 127.0.0.1:{proxy_port + 1000}\n"

            temp_conf = TEMP_VPN_DIR / f"wg_global_{proxy_port}.conf"
            temp_conf.write_text(wireproxy_conf, encoding="utf-8")

            wireproxy_bin = BIN_DIR / ("wireproxy.exe" if os.name == "nt" else "wireproxy")
            if not wireproxy_bin.exists():
                wireproxy_in_path = shutil.which("wireproxy")
                if wireproxy_in_path:
                    wireproxy_bin = Path(wireproxy_in_path)

            if not wireproxy_bin.exists():
                logger.error("wireproxy binary not found in bin/ or PATH. WireGuard mode requires wireproxy.")
                temp_conf.unlink(missing_ok=True)
                return None

            try:
                cmd = [str(wireproxy_bin), "-c", str(temp_conf)]
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self._global_proxy_url = proxy_url
                self._global_vpn_process = VPNProcess(stream_id="global", mode="wireguard", proxy_url=proxy_url, process=proc, temp_file=temp_conf)
                logger.info(f"Started Global WireGuard proxy via wireproxy at {proxy_url}")
                return proxy_url
            except Exception as e:
                logger.error(f"Failed to launch wireproxy for global VPN: {e}")
                temp_conf.unlink(missing_ok=True)
                return None

        return None

    def stop_global_vpn(self):
        """Stop global VPN proxy process or bridge if active."""
        if self._global_vpn_process:
            try:
                self._global_vpn_process.stop()
            except Exception:
                pass
            self._global_vpn_process = None
            logger.info("Stopped Global VPN proxy process.")
        self._global_proxy_url = None

    def get_global_proxy_url(self) -> Optional[str]:
        """Return active global proxy URL if running, else None."""
        return self._global_proxy_url

    def get_proxy_url_for_stream(self, stream_item: dict) -> Optional[str]:
        """
        Return the global proxy URL if the stream item has VPN enabled, else None.
        """
        use_vpn = stream_item.get("use_vpn")
        if use_vpn is None:
            # Backward compatibility check for existing config items
            use_vpn = stream_item.get("vpn_mode", "none") != "none"

        if use_vpn:
            if not self._global_proxy_url:
                self.start_global_vpn()
            return self._global_proxy_url

        return None

    def start_vpn_for_stream(self, stream_id: str, stream_item: dict) -> Optional[str]:
        """Alias for get_proxy_url_for_stream for backward compatibility."""
        return self.get_proxy_url_for_stream(stream_item)

    def stop_vpn_for_stream(self, stream_id: str):
        """No-op for per-stream VPN stop, as VPN is managed globally."""
        pass

    def kill_all_vpn_processes(self):
        """Forcefully kill any running wireproxy.exe processes to prevent lingering orphans."""
        if os.name == "nt":
            import subprocess as _sp
            try:
                _sp.run(
                    ["taskkill", "/F", "/IM", "wireproxy.exe"],
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                    creationflags=_sp.CREATE_NO_WINDOW,
                )
            except Exception:
                pass

    def stop_all(self):
        self.stop_global_vpn()
        self.kill_all_vpn_processes()
        self.purge_temp_dir()

    def purge_temp_dir(self):
        """Purge all temporary files in TEMP_VPN_DIR and terminate leftover VPN processes."""
        self.kill_all_vpn_processes()
        if TEMP_VPN_DIR.exists():
            for item in TEMP_VPN_DIR.iterdir():
                try:
                    if item.is_file():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass


    def get_status(self) -> dict:
        """Return current status of Global VPN manager."""
        cfg = load_config()
        global_vpn = getattr(cfg.streamer, "global_vpn", {}) or {}
        mode = global_vpn.get("mode", "none")
        is_active = self._global_proxy_url is not None
        status = "disabled"
        if mode != "none":
            status = "active" if is_active else "inactive"

        return {
            "mode": mode,
            "active": is_active,
            "proxy_url": self._global_proxy_url or "",
            "status": status,
        }


vpn_manager = VPNManager()

