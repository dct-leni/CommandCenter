"""
Browser manager for Web Stream Live Relays.
Opens target URLs in system default web browser without hardcoded executable paths.
"""

import os
import ctypes
import logging
import webbrowser
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# Directory for isolated browser user profile data
BROWSER_PROFILES_DIR = Path(__file__).parent.parent / "temp" / "browser_profiles"


if os.name == "nt":
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def get_child_pids(parent_pid: int) -> set:
    """Recursively collect parent PID and all descendant child PIDs."""
    pids = {parent_pid}
    if os.name != "nt":
        return pids
    try:
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD),
                ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
                ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wintypes.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)
            ]

        hSnapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnapshot == -1 or hSnapshot == 0:
            return pids

        pe32 = PROCESSENTRY32W()
        pe32.dwSize = ctypes.sizeof(PROCESSENTRY32W)

        if kernel32.Process32FirstW(hSnapshot, ctypes.byref(pe32)):
            while True:
                if pe32.th32ParentProcessID in pids:
                    pids.add(pe32.th32ProcessID)
                if not kernel32.Process32NextW(hSnapshot, ctypes.byref(pe32)):
                    break
        kernel32.CloseHandle(hSnapshot)
    except Exception as e:
        logger.debug(f"Error querying child PIDs: {e}")
    return pids


def get_window_titles_for_pids(target_pids: set) -> List[str]:
    """Get all non-empty window titles belonging to target PIDs."""
    if os.name != "nt" or not target_pids:
        return []
    try:
        from ctypes import wintypes
        titles = []

        def foreach_window(hwnd, lParam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in target_pids:
                buff = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buff, 512)
                val = buff.value
                if val and val.strip():
                    titles.append(val.strip())
            return True

        cb = WNDENUMPROC(foreach_window)
        user32.EnumWindows(cb, 0)
        return titles
    except Exception as e:
        logger.debug(f"Error getting window titles for PIDs: {e}")
        return []


def get_open_window_titles() -> List[str]:
    """Retrieve list of currently open window titles on Windows."""
    if os.name != "nt":
        return []
    try:
        titles = []
        def foreach_window(hwnd, lParam):
            buff = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buff, 512)
            val = buff.value
            if val and val.strip():
                titles.append(val.strip())
            return True

        cb = WNDENUMPROC(foreach_window)
        user32.EnumWindows(cb, 0)
        return titles
    except Exception as e:
        logger.warning(f"Failed to enumerate window titles via ctypes: {e}")
        return []


def find_browser_executable() -> Optional[str]:
    """Find available Chromium-based browser binary path on host Windows system."""
    import shutil
    for name in ["chrome", "msedge", "brave", "opera", "vivaldi"]:
        path = shutil.which(name)
        if path:
            return path

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


class WebStreamManager:
    def __init__(self):
        self.browser_processes: Dict[str, subprocess.Popen] = {}
        self.window_titles: Dict[str, str] = {}

    def launch_browser(self, stream_id: str, name: str, url: str, proxy_url: Optional[str] = None) -> str:
        """
        Launch a separate 1280x720 popup browser window for a web stream directly to the target URL.
        """
        self.close_browser(stream_id)

        profile_dir = BROWSER_PROFILES_DIR / stream_id
        profile_dir.mkdir(parents=True, exist_ok=True)

        browser_exe = find_browser_executable()
        if browser_exe:
            cmd = [
                browser_exe,
                f"--app={url}",
                "--window-size=1280,720",
                f"--user-data-dir={profile_dir.resolve()}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-direct-composition",
                "--block-new-web-contents",
                "--disable-popup-blocking",
                "--disable-notifications",
                "--disable-save-password-bubble",
                "--disable-infobars",
                "--deny-permission-prompts",
                "--disable-translate",
            ]
            if proxy_url:
                cmd.append(f"--proxy-server={proxy_url}")

            logger.info(f"Launching separate popup browser window for web stream '{name}' ({stream_id}) directly to '{url}' using: {browser_exe}")
            proc = subprocess.Popen(
                cmd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.browser_processes[stream_id] = proc
        else:
            logger.info(f"Fallback: opening web stream '{name}' ({stream_id}) via default browser registration: {url}")
            try:
                webbrowser.open_new(url)
            except Exception:
                webbrowser.open(url)

        return name

    def wait_for_window_title(self, stream_id: str, stream_name: str, url: str, timeout: float = 10.0) -> str:
        """Poll system open window titles for up to `timeout` seconds until Chrome renders the target window title."""
        import time
        start_time = time.time()
        
        proc = self.browser_processes.get(stream_id)
        target_pids = set()
        if proc and proc.pid:
            target_pids = get_child_pids(proc.pid)

        parsed_domain = ""
        full_netloc = ""
        try:
            parsed = urlparse(url)
            full_netloc = parsed.netloc.replace("www.", "").strip()
            parsed_domain = full_netloc.split(".")[0]
        except Exception:
            pass

        while time.time() - start_time < timeout:
            # 1. Query exact window titles belonging to the launched Chrome process tree
            if target_pids:
                pid_titles = get_window_titles_for_pids(target_pids)
                for t in pid_titles:
                    if t and t.lower() not in ("chrome", "google chrome", "about:blank", "new tab"):
                        logger.info(f"Detected exact Chrome window title by PID for stream '{stream_name}' ({stream_id}): '{t}'")
                        self.window_titles[stream_id] = t
                        return t

            # 2. Fallback: search open system window titles
            titles = get_open_window_titles()
            if full_netloc:
                for t in titles:
                    if full_netloc.lower() in t.lower():
                        logger.info(f"Detected open Chrome window matching netloc '{full_netloc}': '{t}'")
                        self.window_titles[stream_id] = t
                        return t
            if parsed_domain:
                for t in titles:
                    if parsed_domain.lower() in t.lower():
                        logger.info(f"Detected open Chrome window matching domain '{parsed_domain}': '{t}'")
                        self.window_titles[stream_id] = t
                        return t
            if stream_name:
                for t in titles:
                    if stream_name.lower() in t.lower():
                        logger.info(f"Detected open Chrome window matching stream name '{stream_name}': '{t}'")
                        self.window_titles[stream_id] = t
                        return t
            time.sleep(0.5)

        fallback = full_netloc if full_netloc else (parsed_domain if parsed_domain else stream_name)
        logger.info(f"Window title poll finished; using GDIGrab title: '{fallback}'")
        self.window_titles[stream_id] = fallback
        return fallback

    def get_window_title(self, stream_id: str, default_name: str = "", url: str = "") -> str:
        """Get expected window title for GDIGrab window capture."""
        if stream_id in self.window_titles:
            return self.window_titles[stream_id]
        return self.wait_for_window_title(stream_id, default_name, url, timeout=5.0)

    def close_browser(self, stream_id: str):
        """Clean up state and close browser process for stream_id."""
        proc = self.browser_processes.pop(stream_id, None)
        self.window_titles.pop(stream_id, None)
        if proc:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        profile_dir = BROWSER_PROFILES_DIR / stream_id
        if profile_dir.exists():
            import shutil
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass

    def purge_all(self):
        """Purge all browser processes and temporary profiles in BROWSER_PROFILES_DIR."""
        for stream_id in list(self.browser_processes.keys()):
            self.close_browser(stream_id)

        self.window_titles.clear()
        if BROWSER_PROFILES_DIR.exists():
            import shutil
            for item in BROWSER_PROFILES_DIR.iterdir():
                try:
                    if item.is_file():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass


web_stream_manager = WebStreamManager()
