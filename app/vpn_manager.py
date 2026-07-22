"""
VPN and Proxy Manager for CommandCenter Live Relay Streams.
Manages isolated user-space VPN proxy subprocesses (WireGuard via wireproxy,
OpenVPN, or custom SOCKS5/HTTP proxies) so individual live streams can be
ingested over VPN without altering system network gateways or throttling
local client network connections.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from app.config import load_config

logger = logging.getLogger(__name__)

# Base directory for storing temporary runtime config files
BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
TEMP_VPN_DIR = BASE_DIR / "temp_vpn"
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
        else:
            profile_name = stream_item.get("vpn_profile_name", "")
            profile_content = stream_item.get("vpn_profile_content", "")
            proxy_url = stream_item.get("proxy_url", "")

        return {
            "mode": mode,
            "profile_name": profile_name,
            "profile_content": profile_content,
            "proxy_url": proxy_url,
        }

    def start_vpn_for_stream(self, stream_id: str, stream_item: dict) -> Optional[str]:
        """
        Start VPN/proxy for a stream if configured.
        Returns the local proxy URL (e.g. 'http://127.0.0.1:10501' or 'socks5://127.0.0.1:10501')
        or None if no VPN is enabled.
        """
        # Stop existing VPN process for this stream if running
        self.stop_vpn_for_stream(stream_id)

        eff = self.get_effective_vpn_config(stream_item)
        mode = eff["mode"]

        if mode == "none" or not mode:
            return None

        if mode == "proxy":
            proxy_url = eff.get("proxy_url", "").strip()
            if not proxy_url:
                logger.warning(f"Stream {stream_id} set to proxy mode but proxy_url is empty.")
                return None
            vpn_proc = VPNProcess(stream_id=stream_id, mode="proxy", proxy_url=proxy_url)
            self._active_vpns[stream_id] = vpn_proc
            logger.info(f"Stream {stream_id} using direct proxy: {proxy_url}")
            return proxy_url

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

        if mode == "openvpn":
            content = eff.get("profile_content", "").strip()
            proxy_url = eff.get("proxy_url", "").strip()

            if content:
                proxy_port = self._allocate_port()
                temp_ovpn = TEMP_VPN_DIR / f"ovpn_{stream_id}_{proxy_port}.ovpn"
                temp_ovpn.write_text(content, encoding="utf-8")

                openvpn_bin = BIN_DIR / ("openvpn.exe" if os.name == "nt" else "openvpn")
                if not openvpn_bin.exists():
                    possible_paths = [
                        Path("C:/Program Files/OpenVPN/bin/openvpn.exe"),
                        Path("C:/Program Files (x86)/OpenVPN/bin/openvpn.exe"),
                        Path("C:/Program Files/OpenVPN Connect/openvpn.exe"),
                        Path("C:/Program Files/OpenVPN Connect/ovpncli.exe"),
                        Path.home() / "AppData/Local/Programs/OpenVPN/bin/openvpn.exe",
                    ]
                    for p in possible_paths:
                        if p.exists():
                            openvpn_bin = p
                            break
                    if not openvpn_bin.exists():
                        in_path = shutil.which("openvpn") or shutil.which("openvpn.exe")
                        if in_path:
                            openvpn_bin = Path(in_path)

                if not openvpn_bin.exists():
                    logger.warning("openvpn.exe binary not found in bin/ or system OpenVPN install directory. Using direct/proxy mode.")
                    temp_ovpn.unlink(missing_ok=True)
                    if proxy_url:
                        vpn_proc = VPNProcess(stream_id=stream_id, mode="openvpn", proxy_url=proxy_url)
                        self._active_vpns[stream_id] = vpn_proc
                        return proxy_url
                    return None

                try:
                    cmd = [str(openvpn_bin), "--config", str(temp_ovpn), "--route-nopull"]
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    vpn_proc = VPNProcess(stream_id=stream_id, mode="openvpn", proxy_url=proxy_url, process=proc, temp_file=temp_ovpn)
                    self._active_vpns[stream_id] = vpn_proc
                    logger.info(f"Started OpenVPN process for stream '{stream_id}' using configuration {temp_ovpn.name}")
                    return proxy_url if proxy_url else None
                except Exception as e:
                    logger.error(f"Failed to launch openvpn for stream {stream_id}: {e}")
                    temp_ovpn.unlink(missing_ok=True)
                    return None
            elif proxy_url:
                vpn_proc = VPNProcess(stream_id=stream_id, mode="openvpn", proxy_url=proxy_url)
                self._active_vpns[stream_id] = vpn_proc
                return proxy_url

        return None

    def stop_vpn_for_stream(self, stream_id: str):
        if stream_id in self._active_vpns:
            vpn_proc = self._active_vpns.pop(stream_id)
            vpn_proc.stop()
            logger.info(f"Stopped VPN proxy process for stream '{stream_id}'")

    def stop_all(self):
        for stream_id in list(self._active_vpns.keys()):
            self.stop_vpn_for_stream(stream_id)


vpn_manager = VPNManager()
