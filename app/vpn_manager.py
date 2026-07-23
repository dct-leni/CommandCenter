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
    Lightweight local HTTP-to-SOCKS5/HTTP proxy bridge.
    Accepts unauthenticated HTTP/CONNECT requests from Chrome/FFmpeg on 127.0.0.1:<port>
    and forwards them to a remote authenticated SOCKS5 or HTTP proxy server (e.g. ZoogVPN SOCKS5).
    This solves Chromium's ERR_NO_SUPPORTED_PROXIES error when credentials are present in --proxy-server.
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

    def start(self):
        import socket
        import threading
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", self.local_port))
        self.server_sock.listen(100)
        self.running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        import time
        time.sleep(0.3)  # Pre-warm sleep to guarantee server_sock accept loop is active
        logger.info(f"Started local proxy bridge at http://127.0.0.1:{self.local_port} -> {self.scheme}://{self.remote_host}:{self.remote_port}")

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _listen_loop(self):
        import threading
        while self.running:
            try:
                client_sock, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except Exception:
                break

    def _handle_client(self, client_sock):
        import socket
        import socks
        try:
            client_sock.settimeout(15.0)
            req_data = client_sock.recv(8192)
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
            proxy_type = socks.SOCKS5 if "socks" in self.scheme else socks.HTTP
            remote_sock.set_proxy(
                proxy_type,
                self.remote_host,
                self.remote_port,
                username=self.username if self.username else None,
                password=self.password if self.password else None,
                rdns=True,  # Crucial for remote SOCKS5 domain name resolution
            )
            remote_sock.settimeout(30.0)

            if method == "CONNECT":
                host_port = target.split(":")
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 443
                
                remote_sock.connect((host, port))
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._pipe(client_sock, remote_sock)
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

                remote_sock.connect((host, port))
                remote_sock.sendall(req_data)
                self._pipe(client_sock, remote_sock)
        except Exception as e:
            logger.debug(f"LocalProxyBridge connection handling error: {e}")
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def _pipe(self, s1, s2):
        import socket
        import threading
        s1.settimeout(60.0)
        s2.settimeout(60.0)

        def _forward(src, dst):
            try:
                while self.running:
                    data = src.recv(32768)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=_forward, args=(s1, s2), daemon=True)
        t2 = threading.Thread(target=_forward, args=(s2, s1), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            s1.close()
        except Exception:
            pass
        try:
            s2.close()
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
    """Singleton managing VPN proxy processes per stream or globally."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VPNManager, cls).__new__(cls)
            cls._instance._active_vpns: Dict[str, VPNProcess] = {}
            cls._instance._port_counter = 10500
        return cls._instance

    def _allocate_port(self) -> int:
        self._port_counter += 1
        return self._port_counter

    def get_effective_vpn_config(self, stream_item: dict) -> dict:
        """
        Determine effective VPN configuration for a given stream item.
        Respects stream-specific settings if present, otherwise falls back to global settings.
        """
        cfg = load_config()
        global_vpn = getattr(cfg.streamer, "global_vpn", {}) or {}

        mode = stream_item.get("vpn_mode", "none")
        if mode == "global":
            mode = global_vpn.get("mode", "none")
            profile_name = global_vpn.get("profile_name", "")
            profile_content = global_vpn.get("profile_content", "")
            proxy_url = global_vpn.get("proxy_url", "")
            proxy_username = global_vpn.get("proxy_username", "")
            proxy_password = global_vpn.get("proxy_password", "")
        else:
            profile_name = stream_item.get("vpn_profile_name", "")
            profile_content = stream_item.get("vpn_profile_content", "")
            proxy_url = stream_item.get("proxy_url", "")
            proxy_username = stream_item.get("proxy_username", "")
            proxy_password = stream_item.get("proxy_password", "")

        return {
            "mode": mode,
            "profile_name": profile_name,
            "profile_content": profile_content,
            "proxy_url": proxy_url,
            "proxy_username": proxy_username,
            "proxy_password": proxy_password,
        }

    def start_vpn_for_stream(self, stream_id: str, stream_item: dict) -> Optional[str]:
        """
        Start VPN/proxy for a stream if configured.
        Returns the local proxy URL (e.g. 'http://127.0.0.1:10501')
        or None if no VPN is enabled.
        """
        # Stop existing VPN process for this stream if running
        self.stop_vpn_for_stream(stream_id)

        eff = self.get_effective_vpn_config(stream_item)
        mode = eff["mode"]

        if mode == "none" or not mode:
            return None

        if mode == "proxy":
            raw_url = eff.get("proxy_url", "")
            user = eff.get("proxy_username", "")
            pwd = eff.get("proxy_password", "")
            host, port, scheme, user, pwd = parse_proxy_parts(raw_url, user, pwd)
            
            if not host:
                logger.warning(f"Stream {stream_id} set to proxy mode but proxy_url host is empty.")
                return None

            # Always run a local proxy bridge on 127.0.0.1 so Chromium receives a clean unauthenticated --proxy-server flag
            local_port = self._allocate_port()
            bridge = LocalProxyBridge(local_port, host, port, scheme, user, pwd)
            bridge.start()

            local_proxy_url = f"http://127.0.0.1:{local_port}"
            vpn_proc = VPNProcess(stream_id=stream_id, mode="proxy", proxy_url=local_proxy_url, bridge=bridge)
            self._active_vpns[stream_id] = vpn_proc
            logger.info(f"Stream {stream_id} active via local proxy bridge {local_proxy_url} -> {scheme}://{host}:{port}")
            return local_proxy_url

        if mode == "wireguard":
            content = eff.get("profile_content", "").strip()
            if not content:
                logger.warning(f"Stream {stream_id} set to WireGuard mode but profile content is empty.")
                return None

            proxy_port = self._allocate_port()
            proxy_url = f"http://127.0.0.1:{proxy_port}"

            # Append wireproxy HTTP/SOCKS5 server config if not present in the .conf
            wireproxy_conf = content
            if "[Socks5]" not in wireproxy_conf and "[HTTP]" not in wireproxy_conf:
                wireproxy_conf += f"\n\n[HTTP]\nBindAddress = 127.0.0.1:{proxy_port}\n"
                wireproxy_conf += f"\n[Socks5]\nBindAddress = 127.0.0.1:{proxy_port + 1000}\n"

            # Write temporary .conf file
            temp_conf = TEMP_VPN_DIR / f"wg_{stream_id}_{proxy_port}.conf"
            temp_conf.write_text(wireproxy_conf, encoding="utf-8")

            # Find wireproxy binary
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
                vpn_proc = VPNProcess(stream_id=stream_id, mode="wireguard", proxy_url=proxy_url, process=proc, temp_file=temp_conf)
                self._active_vpns[stream_id] = vpn_proc
                logger.info(f"Started WireGuard proxy via wireproxy for stream '{stream_id}' at {proxy_url}")
                return proxy_url
            except Exception as e:
                logger.error(f"Failed to launch wireproxy for stream {stream_id}: {e}")
                temp_conf.unlink(missing_ok=True)
                return None

        return None

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

    def stop_vpn_for_stream(self, stream_id: str):
        if stream_id in self._active_vpns:
            vpn_proc = self._active_vpns.pop(stream_id)
            vpn_proc.stop()
            logger.info(f"Stopped VPN proxy process for stream '{stream_id}'")

        # Clean up any residual temp config files for this stream_id
        for f in TEMP_VPN_DIR.glob(f"*{stream_id}*"):
            try:
                if f.is_file():
                    f.unlink(missing_ok=True)
                elif f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
            except Exception:
                pass

    def stop_all(self):
        for stream_id in list(self._active_vpns.keys()):
            self.stop_vpn_for_stream(stream_id)
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


vpn_manager = VPNManager()

