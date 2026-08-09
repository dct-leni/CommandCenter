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
BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
TEMP_VPN_DIR = BASE_DIR / "temp"
TEMP_VPN_DIR.mkdir(exist_ok=True)


class VPNProcess:
    def __init__(self, stream_id: str, mode: str, proxy_url: str, process: Optional[subprocess.Popen] = None, temp_file: Optional[Path] = None):
        self.stream_id = stream_id
        self.mode = mode
        self.proxy_url = proxy_url
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
                self.temp_file.unlink()
            except Exception:
                pass


class VPNManager:
    """Singleton managing global WireGuard VPN proxy processes."""

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
        Start Global WireGuard VPN if configured in global settings.
        Returns local proxy URL (e.g. 'http://127.0.0.1:10501') or None if disabled.
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

            proxy_port = self._allocate_port()
            proxy_url = f"http://127.0.0.1:{proxy_port}"

            wireproxy_conf = content

            if "[HTTP]" not in wireproxy_conf:
                wireproxy_conf += f"\n\n[HTTP]\nBindAddress = 127.0.0.1:{proxy_port}\n"

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
                log_file = TEMP_VPN_DIR / f"wg_global_{proxy_port}.log"
                log_fd = open(log_file, "w")
                cmd = [str(wireproxy_bin), "-c", str(temp_conf)]
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self._global_proxy_url = proxy_url
                self._global_vpn_process = VPNProcess(stream_id="global", mode="wireguard", proxy_url=proxy_url, process=proc, temp_file=temp_conf)
                logger.info(f"Started Global WireGuard VPN tunnel (local bridge at {proxy_url})")
                return proxy_url
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

    def get_global_proxy_url(self) -> Optional[str]:
        """Return active global proxy URL if running, else None."""
        return self._global_proxy_url

    def get_proxy_url_for_stream(self, stream_item: dict) -> Optional[str]:
        """
        Return the global proxy URL if the stream item has VPN enabled, else None.
        """
        use_vpn = stream_item.get("use_vpn")
        if use_vpn is None:
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

        is_alive = False
        if self._global_vpn_process and self._global_vpn_process.process:
            is_alive = (self._global_vpn_process.process.poll() is None)

        status_str = "active" if is_alive else "error"

        return {
            "mode": "wireguard",
            "active": is_alive,
            "proxy_url": self._global_proxy_url if is_alive else "",
            "status": status_str,
        }


vpn_manager = VPNManager()

