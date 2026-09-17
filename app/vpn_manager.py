"""
VPN Manager for CommandCenter Live Relay Streams.
Manages isolated user-space WireGuard proxy subprocesses (via wireproxy)
so individual live streams or browsers can be ingested over WireGuard
without altering system network gateways or throttling local client connections.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.config import load_config

logger = logging.getLogger(__name__)

# Base directory for storing temporary runtime config files
import time
import urllib.request

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
TEMP_VPN_DIR = BASE_DIR / "temp"
TEMP_VPN_DIR.mkdir(exist_ok=True)


class VPNProcess:
    def __init__(
        self,
        stream_id: str,
        mode: str,
        http_url: str,
        socks_url: str = "",
        info_url: str = "",
        http_port: int = 0,
        socks_port: int = 0,
        info_port: int = 0,
        process: Optional[subprocess.Popen] = None,
        temp_file: Optional[Path] = None,
    ):
        self.stream_id = stream_id
        self.mode = mode
        self.http_url = http_url
        self.socks_url = socks_url
        self.info_url = info_url
        self.http_port = http_port
        self.socks_port = socks_port
        self.info_port = info_port
        self.proxy_url = http_url  # Backward compatibility
        self.process = process
        self.temp_file = temp_file

    def stop(self):
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
                self.temp_file.unlink(missing_ok=True)
            except Exception:
                pass


class VPNManager:
    """Singleton managing high-performance global WireGuard VPN proxy processes."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VPNManager, cls).__new__(cls)
            cls._instance._global_vpn_process: Optional[VPNProcess] = None
            cls._instance._global_proxy_url: Optional[str] = None
            cls._instance._global_socks_url: Optional[str] = None
            cls._instance._global_info_url: Optional[str] = None
            cls._instance._port_counter = 10500
            cls._instance._metrics_cache = {}
            cls._instance._metrics_cache_time = 0.0
        return cls._instance

    def _allocate_port(self) -> int:
        """Allocate a single unique local port."""
        return self._allocate_ports(1)[0]

    def _allocate_ports(self, count: int = 3) -> tuple:
        """Allocate a contiguous block of unique local ports (HTTP, SOCKS5, Info)."""
        base = self._port_counter + 1
        self._port_counter += count
        return tuple(base + i for i in range(count))

    @staticmethod
    def _optimize_wireguard_conf(content: str, http_port: int, socks_port: int) -> str:
        """
        Sanitize and configure WireGuard for wireproxy:
        1. Injects PersistentKeepalive = 25 into [Peer] if missing.
        2. Configures [HTTP] and [Socks5] listeners on local ports.
        Preserves user's MTU, IP, and cryptographic keys verbatim without alteration.
        """
        lines = content.replace("\r\n", "\n").split("\n")
        output_lines = []
        current_section = None
        section_lines = []

        def flush_section(sec, s_lines):
            if not sec:
                output_lines.extend(s_lines)
                return
            sec_lower = sec.lower().strip()
            # Strip any preexisting manual proxy sections to avoid duplicate binds
            if sec_lower in ("[http]", "[socks5]"):
                return

            output_lines.append(sec)
            if sec_lower == "[peer]":
                keepalive_found = False
                for line in s_lines:
                    trimmed = line.strip()
                    if trimmed.lower().startswith("persistentkeepalive"):
                        keepalive_found = True
                    output_lines.append(line)
                if not keepalive_found:
                    output_lines.append("PersistentKeepalive = 25")
            else:
                output_lines.extend(s_lines)

        for raw_line in lines:
            trimmed = raw_line.strip()
            if trimmed.startswith("[") and trimmed.endswith("]"):
                flush_section(current_section, section_lines)
                current_section = trimmed
                section_lines = []
            else:
                section_lines.append(raw_line)
        flush_section(current_section, section_lines)

        # Append Dual Protocol bridges (HTTP on http_port, SOCKS5 on socks_port)
        output_lines.append("")
        output_lines.append("[HTTP]")
        output_lines.append(f"BindAddress = 127.0.0.1:{http_port}")
        output_lines.append("")
        output_lines.append("[Socks5]")
        output_lines.append(f"BindAddress = 127.0.0.1:{socks_port}")
        output_lines.append("")

        return "\n".join(output_lines)

    def start_global_vpn(self) -> Optional[str]:
        """
        Start Global WireGuard VPN if configured in global settings.
        Returns local HTTP proxy URL (e.g. 'http://127.0.0.1:10501') or None if disabled.
        """
        self.stop_global_vpn()

        cfg = load_config()
        global_vpn = getattr(cfg.streamer, "global_vpn", {}) or {}
        mode = global_vpn.get("mode", "none")

        if mode == "none" or not mode:
            return None

        if mode == "wireguard":
            content = global_vpn.get("profile_content", "").strip()
            if not content:
                logger.warning("Global VPN set to WireGuard mode but profile content is empty.")
                return None

            http_port, socks_port, info_port = self._allocate_ports(3)
            http_url = f"http://127.0.0.1:{http_port}"
            socks_url = f"socks5://127.0.0.1:{socks_port}"
            info_url = f"http://127.0.0.1:{info_port}"

            wireproxy_conf = self._optimize_wireguard_conf(content, http_port, socks_port)

            temp_conf = TEMP_VPN_DIR / f"wg_global_{http_port}.conf"
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
                cmd = [
                    str(wireproxy_bin),
                    "-s",
                    "-c", str(temp_conf),
                ]
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self._global_proxy_url = http_url
                self._global_socks_url = socks_url
                self._global_info_url = info_url
                self._global_vpn_process = VPNProcess(
                    stream_id="global",
                    mode="wireguard",
                    http_url=http_url,
                    socks_url=socks_url,
                    info_url=info_url,
                    http_port=http_port,
                    socks_port=socks_port,
                    info_port=info_port,
                    process=proc,
                    temp_file=temp_conf,
                )
                logger.info(f"Started Global WireGuard VPN tunnel (HTTP: {http_url}, SOCKS5: {socks_url})")
                return http_url
            except Exception as e:
                logger.error(f"Failed to launch wireproxy for global VPN: {e}")
                temp_conf.unlink(missing_ok=True)
                return None

        logger.warning(f"Unsupported or removed VPN mode requested: {mode}")
        return None

    def stop_global_vpn(self):
        """Stop global WireGuard VPN proxy process if active."""
        if self._global_vpn_process:
            try:
                self._global_vpn_process.stop()
            except Exception:
                pass
            self._global_vpn_process = None
            logger.info("Stopped Global VPN proxy process.")
        self._global_proxy_url = None
        self._global_socks_url = None
        self._global_info_url = None
        self._metrics_cache.clear()

    async def wait_until_ready(self, timeout: float = 3.0) -> bool:
        """
        Actively probe local proxy port until accepting connections.
        Returns True immediately once socket connects, or False on timeout/crash.
        Eliminates artificial fixed sleep delays while preventing connection race conditions.
        """
        if not self._global_vpn_process or not self._global_vpn_process.process:
            return False

        http_port = getattr(self._global_vpn_process, "http_port", None)
        if not http_port:
            return False

        import asyncio
        start_time = time.monotonic()
        deadline = start_time + timeout

        while time.monotonic() < deadline:
            # Detect premature termination / misconfiguration
            if self._global_vpn_process.process.poll() is not None:
                stderr_text = ""
                try:
                    if self._global_vpn_process.process.stderr:
                        stderr_text = self._global_vpn_process.process.stderr.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
                logger.error(f"wireproxy exited unexpectedly during startup: {stderr_text.strip()}")
                return False

            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", http_port),
                    timeout=0.2
                )
                writer.close()
                await writer.wait_closed()
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                logger.info(f"WireGuard local proxy ready at 127.0.0.1:{http_port} in {elapsed_ms}ms")
                return True
            except Exception:
                await asyncio.sleep(0.015)

        logger.warning(f"WireGuard local proxy at 127.0.0.1:{http_port} did not become ready within {timeout}s")
        return False

    async def warmup_tunnel(self, timeout: float = 2.5) -> bool:
        """
        Actively pre-warm the WireGuard tunnel on app startup by triggering an initial handshake.
        Uses a lightweight HTTP 204 ping through the local HTTP proxy bridge.
        """
        if not self._global_vpn_process or not self._global_vpn_process.http_port:
            return False

        ready = await self.wait_until_ready(timeout=2.0)
        if not ready:
            return False

        http_port = self._global_vpn_process.http_port
        import asyncio
        import urllib.request

        def _do_warmup():
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({'http': f'http://127.0.0.1:{http_port}'})
                )
                req = urllib.request.Request("http://cp.cloudflare.com/generate_204")
                with opener.open(req, timeout=timeout) as resp:
                    pass
                logger.info("WireGuard VPN tunnel pre-warmed and connected on app startup.")
                return True
            except Exception as e:
                logger.debug(f"WireGuard warmup completed: {e}")
                return False

        return await asyncio.to_thread(_do_warmup)

    def get_global_proxy_url(self, protocol: str = "http") -> Optional[str]:
        """Return active global proxy URL for specified protocol ('http' or 'socks5')."""
        if protocol == "socks5":
            return self._global_socks_url or self._global_proxy_url
        return self._global_proxy_url

    def get_proxy_url_for_stream(self, stream_item: dict, protocol: str = "http") -> Optional[str]:
        """
        Return the global proxy URL (http or socks5) if the stream item has VPN enabled, else None.
        """
        use_vpn = stream_item.get("use_vpn")
        if use_vpn is None:
            use_vpn = stream_item.get("vpn_mode", "none") != "none"

        if use_vpn:
            if not self._global_proxy_url:
                self.start_global_vpn()
            if protocol == "socks5":
                return self._global_socks_url or self._global_proxy_url
            return self._global_proxy_url

        return None

    def start_vpn_for_stream(self, stream_id: str, stream_item: dict, protocol: str = "http") -> Optional[str]:
        """Alias for get_proxy_url_for_stream for backward compatibility."""
        return self.get_proxy_url_for_stream(stream_item, protocol=protocol)

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
                    stdin=_sp.DEVNULL,
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
        """Return actual live status of Global WireGuard VPN manager."""
        cfg = load_config()
        global_vpn = getattr(cfg.streamer, "global_vpn", {}) or {}
        mode = global_vpn.get("mode", "none")
        
        if mode == "none" or not mode or mode != "wireguard":
            return {
                "mode": mode if mode != "wireguard" else "none",
                "active": False,
                "proxy_url": "",
                "status": "disabled",
            }

        is_proc_alive = False
        if self._global_vpn_process and self._global_vpn_process.process:
            is_proc_alive = (self._global_vpn_process.process.poll() is None)

        status_str = "active" if is_proc_alive else "error"

        return {
            "mode": "wireguard",
            "active": is_proc_alive,
            "proxy_url": self._global_proxy_url if is_proc_alive else "",
            "socks_url": getattr(self, "_global_socks_url", "") if is_proc_alive else "",
            "status": status_str,
        }


vpn_manager = VPNManager()


