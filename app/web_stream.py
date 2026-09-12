"""
Browser manager for Web Stream Live Relays.
Launches Portable Firefox (phyrox-portable) with an isolated profile and a
dynamically generated MV2 extension that overrides page visibility (prevents auto-pause).
Audio is routed natively via OS-level WASAPI to VB-Audio Virtual Cable.
"""

import os
import shutil
import ctypes
import logging
import tempfile
import time
import zipfile
import json
import webbrowser
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, List

import threading

logger = logging.getLogger(__name__)

# Global lock to prevent race conditions when generating Portapps YAML config for concurrent streams
_launcher_lock = threading.Lock()

# Base project directory (CommandCenter root)
_BASE_DIR = Path(__file__).parent.parent

# Directory for isolated Firefox profile data per stream
BROWSER_PROFILES_DIR = _BASE_DIR / "temp" / "firefox_profiles"

# Paths for portable Firefox binary candidates inside bin/
_FIREFOX_CANDIDATES = [
    # Direct app binaries (strict multi-process isolation via -no-remote -new-instance)
    _BASE_DIR / "bin" / "firefox" / "app" / "firefox.exe",
    _BASE_DIR / "bin" / "firefox" / "firefox.exe",
    _BASE_DIR / "bin" / "phyrox-portable-win64-152.0.4-70" / "app" / "firefox.exe",
    _BASE_DIR / "bin" / "firefox-win" / "firefox.exe",
    _BASE_DIR / "bin" / "firefox-win" / "app" / "firefox.exe",
    # Phyrox-portable launcher fallbacks
    _BASE_DIR / "bin" / "firefox" / "phyrox-portable.exe",
    _BASE_DIR / "bin" / "phyrox-portable-win64-152.0.4-70" / "phyrox-portable.exe",
    _BASE_DIR / "bin" / "phyrox-portable.exe",
]


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
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    # ClientToScreen takes HWND + POINT pointer — declared lazily to avoid POINT definition issues here


def get_child_pids(parent_pid: int) -> set:
    """Recursively collect parent PID and all descendant child PIDs using psutil."""
    pids = {parent_pid}
    try:
        import psutil
        try:
            parent = psutil.Process(parent_pid)
            children = parent.children(recursive=True)
            for child in children:
                pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except ImportError:
        pass
    return pids


def get_stream_pids(stream_id: str, parent_pid: Optional[int] = None) -> set:
    """
    Collect all PIDs belonging to this stream's Firefox process tree.
    Combines direct child process tree tracking with full psutil command-line matching.
    """
    pids = set()
    if parent_pid:
        pids.add(parent_pid)
        pids.update(get_child_pids(parent_pid))

    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pname = (p.info.get('name') or '').lower()
                if 'firefox' in pname or 'phyrox' in pname:
                    cmdline = p.info.get('cmdline') or []
                    if any(stream_id in str(arg) for arg in cmdline):
                        pids.add(p.info['pid'])
                        try:
                            for child in p.children(recursive=True):
                                pids.add(child.pid)
                        except Exception:
                            pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    return pids


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


def find_firefox_executable() -> Optional[str]:
    """Return path to portable Firefox binary inside bin/."""
    for candidate in _FIREFOX_CANDIDATES:
        if candidate.exists():
            logger.debug(f"Found portable Firefox at: {candidate}")
            return str(candidate)

    # Dynamic fallback: search anywhere inside bin/ for firefox.exe first, then phyrox-portable.exe
    bin_dir = _BASE_DIR / "bin"
    if bin_dir.exists():
        for found in bin_dir.rglob("firefox.exe"):
            logger.info(f"Found firefox.exe via rglob scan at: {found}")
            return str(found)
        for found in bin_dir.rglob("phyrox-portable.exe"):
            logger.info(f"Found phyrox-portable.exe via rglob scan at: {found}")
            return str(found)

    return None


def _ensure_firefox_policies(firefox_exe: Path) -> None:
    """Write Enterprise policies.json to force Firefox to skip Welcome / Terms of Use / Telemetry prompts."""
    try:
        policy_data = {
            "policies": {
                "DisableAppUpdate": True,
                "DisableFeedbackCommands": True,
                "DisableFirefoxStudies": True,
                "DisablePocket": True,
                "DisableTelemetry": True,
                "OverrideFirstRunPage": "",
                "OverridePostUpdatePage": "",
                "SkipFirstRunWelcome": True,
                "UserMessaging": {
                    "ExtensionRecommendations": False,
                    "FeatureRecommendations": False,
                    "UrlbarInteractions": False,
                    "WhatsNew": False,
                    "SkipOnboarding": True
                },
                "Preferences": {
                    "browser.aboutwelcome.enabled": False,
                    "browser.rights.3.shown": True,
                    "browser.rights.override": "show",
                    "browser.rights.silence": True,
                    "browser.tos.accepted": True,
                    "browser.tos.shown": True,
                    "browser.onboarding.enabled": False,
                    "browser.onboarding.hidden": True,
                    "datareporting.policy.dataSubmissionPolicyAcceptedVersion": 999,
                    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
                    "datareporting.policy.firstRunURL": "",
                },
                "DontCheckDefaultBrowser": True,
            }
        }
        policy_json = json.dumps(policy_data, indent=2)

        # Write Enterprise policies.json directly next to the firefox.exe binary
        app_dir = firefox_exe.parent if firefox_exe.name.lower() == "firefox.exe" else firefox_exe.parent / "app"
        dist_dir = app_dir / "distribution"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "policies.json").write_text(policy_json, encoding="utf-8")
        logger.info(f"Created Enterprise policies.json at: {dist_dir / 'policies.json'}")
    except Exception as e:
        logger.warning(f"Could not write Firefox policies.json: {e}")


def _create_firefox_profile(profile_dir: Path, proxy_url: Optional[str] = None, stream_id: str = "") -> None:
    """
    Create (or refresh) an isolated Firefox profile with user.js prefs and the MV2 audio
    extension. On every call:
      - Stale session/cache/cookie files are deleted so Firefox starts clean.
      - Comprehensive user.js prefs silence all first-run dialogs (incl. Terms of Use),
        block popups, and auto-accept cookie banners.
      - The MV2 audio extension is copied fresh into the extensions folder.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)

    # --- Clean stale session/cache/cookie files on every restart ---
    _STALE_PATHS = [
        "sessionstore.jsonlz4",
        "sessionstore-backups",
        "cookies.sqlite",
        "cookies.sqlite-shm",
        "cookies.sqlite-wal",
        "cache2",
        "startupCache",
        "thumbnails",
        "OfflineCache",
        "storage",
        "webappsstore.sqlite",
        "extensions.json",
        "addonStartup.json.lz4",
        "prefs.js"
    ]
    for name in _STALE_PATHS:
        p = profile_dir / name
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass

    # --- Ensure Widevine CDM is available for DRM content (S Sport Plus, Exxen, Tabii) ---
    gmp_app_widevine = _BASE_DIR / "bin" / "firefox" / "app" / "gmp-widevinecdm"
    if not gmp_app_widevine.exists():
        app_data = os.environ.get("APPDATA")
        if app_data:
            mozilla_profiles = Path(app_data) / "Mozilla" / "Firefox" / "Profiles"
            if mozilla_profiles.exists():
                for found_w in mozilla_profiles.rglob("gmp-widevinecdm"):
                    if found_w.is_dir() and any(found_w.iterdir()):
                        try:
                            shutil.copytree(found_w, gmp_app_widevine, dirs_exist_ok=True)
                            logger.info(f"Copied Widevine CDM to {gmp_app_widevine}")
                            break
                        except Exception:
                            pass
    if gmp_app_widevine.exists():
        try:
            shutil.copytree(gmp_app_widevine, profile_dir / "gmp-widevinecdm", dirs_exist_ok=True)
        except Exception:
            pass

    assets_dir = Path(__file__).parent / "assets"
    ext_assets = assets_dir / "extension"
    ff_assets = assets_dir / "firefox"

    # --- Generate Page Visibility Override Extension ---
    # This prevents sites like YouTube/Twitch/TikTok from pausing video when unfocused
    ext_dir = profile_dir / "extensions"
    ext_dir.mkdir(exist_ok=True)
    ext_id = "visibility@commandcenter.local"
    ext_path = ext_dir / f"{ext_id}.xpi"

    manifest_text = (ext_assets / "manifest.json").read_text(encoding="utf-8")
    content_js = (ext_assets / "content.js").read_text(encoding="utf-8")

    with zipfile.ZipFile(ext_path, 'w') as zf:
        zf.writestr('manifest.json', manifest_text)
        zf.writestr('content.js', content_js.replace("{stream_id}", stream_id))

    # --- Write user.js preferences ---
    user_js = profile_dir / "user.js"
    base_prefs = (ff_assets / "user.js").read_text(encoding="utf-8").splitlines()
    prefs = []
    for p in base_prefs:
        s = p.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        if "';" in s:
            s = s.split("';")[0].strip()
            if not s.endswith(";"):
                s += ";"
        prefs.append(s)

    # --- Proxy Configuration (SOCKS5 or HTTP) ---
    if proxy_url:
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(proxy_url)
            scheme = parsed.scheme.lower()
            host = parsed.hostname
            port = parsed.port or 1080

            if scheme in ("socks5", "socks5h"):
                prefs.extend([
                    'user_pref("network.proxy.type", 1);',
                    f'user_pref("network.proxy.socks", "{host}");',
                    f'user_pref("network.proxy.socks_port", {port});',
                    'user_pref("network.proxy.socks_version", 5);',
                    'user_pref("network.proxy.socks_remote_dns", true);',
                ])
            else:
                prefs.extend([
                    'user_pref("network.proxy.type", 1);',
                    f'user_pref("network.proxy.http", "{host}");',
                    f'user_pref("network.proxy.http_port", {port});',
                    f'user_pref("network.proxy.ssl", "{host}");',
                    f'user_pref("network.proxy.ssl_port", {port});',
                    'user_pref("network.proxy.share_proxy_settings", true);',
                    'user_pref("network.proxy.no_proxies_on", "");',
                ])
            logger.info(f"Applied Firefox proxy settings for {proxy_url}")
        except Exception as e:
            logger.warning(f"Failed to parse proxy_url {proxy_url}: {e}")
    else:
        prefs.append('user_pref("network.proxy.type", 0);')

    user_js.write_text("\n".join(prefs) + "\n", encoding="utf-8")

    # --- Write userChrome.css and userContent.css ---
    chrome_dir = profile_dir / "chrome"
    chrome_dir.mkdir(exist_ok=True)
    shutil.copy2(ff_assets / "userChrome.css", chrome_dir / "userChrome.css")
    shutil.copy2(ff_assets / "userContent.css", chrome_dir / "userContent.css")


class WebStreamManager:
    def __init__(self):
        self.browser_processes: Dict[str, subprocess.Popen] = {}
        self.window_titles: Dict[str, str] = {}
        self.window_hwnds: Dict[str, int] = {}  # HWND of the Firefox content window

    def launch_browser(self, stream_id: str, name: str, url: str, proxy_url: Optional[str] = None, resolution: str = "720p") -> str:
        """
        Launch a Portable Firefox popup for a web stream with requested resolution (720p or 1080p).
        Creates an isolated profile with the CommandCenter MV2 audio extension pre-loaded.
        """
        self.close_browser(stream_id)

        is_1080p = "1080" in str(resolution or "").lower()
        w_px = 1920 if is_1080p else 1280
        h_px = 1080 if is_1080p else 720

        firefox_exe = find_firefox_executable()
        if firefox_exe:
            exe_path = Path(firefox_exe)
            
            if exe_path.name.lower() == "phyrox-portable.exe":
                # Portapps strictly resolves profiles relative to data/profile/
                profile_dir = exe_path.parent / "data" / "profile" / stream_id
                _create_firefox_profile(profile_dir, proxy_url, stream_id)
                
                yaml_path = exe_path.with_suffix(".yml")
                yml_content = f"""common:
  disable_log: true
  args: []
  env: {{}}
  app_path: ""
app:
  profile: "{stream_id}"
  multiple_instances: true
  disable_telemetry: true
  disable_firefox_studies: true
  disable_crash_reporter: true
  locale: en-US
  cleanup: true
"""
                with _launcher_lock:
                    _ensure_firefox_policies(exe_path)
                    yaml_path.write_text(yml_content, encoding="utf-8")
                    cmd = [
                        firefox_exe,
                        f"--width={w_px}",
                        f"--height={h_px}",
                        url,
                        "-foreground"
                    ]
                    env = os.environ.copy()
                    env["TZ"] = "Europe/Istanbul"
                    logger.info(f"Launching Portapps phyrox-portable for '{name}' ({stream_id}) [{w_px}x{h_px}] -> '{url}'")
                    proc = subprocess.Popen(
                        cmd,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
                    )
                    # Brief sleep to ensure Portapps reads the YAML before another thread can overwrite it
                    time.sleep(1.0)
            else:
                profile_dir = BROWSER_PROFILES_DIR / stream_id
                _create_firefox_profile(profile_dir, proxy_url, stream_id)
                
                _ensure_firefox_policies(exe_path)
                cmd = [
                    firefox_exe,
                    "--no-remote",
                    "--new-instance",
                    f"--profile", str(profile_dir.resolve()),
                    f"--width={w_px}",
                    f"--height={h_px}",
                    url,
                    "-foreground"
                ]
                env = os.environ.copy()
                env["TZ"] = "Europe/Istanbul"
                logger.info(f"Launching Native Firefox for web stream '{name}' ({stream_id}) [{w_px}x{h_px}] -> '{url}'")
                proc = subprocess.Popen(
                    cmd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
                )
            
            self.browser_processes[stream_id] = proc
        else:
            logger.warning(f"Portable Firefox not found. Falling back to default browser for web stream '{name}' ({stream_id})")
            try:
                webbrowser.open_new(url)
            except Exception:
                webbrowser.open(url)

        return name

    def wait_for_window_title(self, stream_id: str, stream_name: str, url: str, timeout: float = 10.0, resolution: str = "720p") -> str:
        """Poll window titles for up to timeout seconds. Also stores the HWND for region-based capture."""
        import time
        start_time = time.time()

        parsed_domain = ""
        full_netloc = ""
        try:
            parsed = urlparse(url)
            full_netloc = parsed.netloc.replace("www.", "").strip()
            parsed_domain = full_netloc.split(".")[0]
        except Exception:
            pass

        _FIREFOX_SKIP = {
            "firefox media keys",
            "about:blank", "new tab", "before you continue",
            "privacy policy", "cookie", "consent",
        }

        while time.time() - start_time < timeout:
            proc = self.browser_processes.get(stream_id)
            target_pids = get_stream_pids(stream_id, proc.pid if proc else None)

            if os.name == "nt":
                found: List[tuple] = []  # (hwnd, title, score)

                def _enum_windows(hwnd, lParam):
                    if user32.IsWindowVisible(hwnd):
                        rect = ctypes.wintypes.RECT()
                        user32.GetClientRect(hwnd, ctypes.byref(rect))
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w >= 400 and h >= 300:
                            cls_buf = ctypes.create_unicode_buffer(256)
                            user32.GetClassNameW(hwnd, cls_buf, 256)
                            cls = cls_buf.value
                            pid = ctypes.wintypes.DWORD()
                            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                            title_buf = ctypes.create_unicode_buffer(512)
                            user32.GetWindowTextW(hwnd, title_buf, 512)
                            val = title_buf.value.strip()

                            # Match 1: PID belongs to this stream's process tree (Direct 100% confidence match)
                            if target_pids and pid.value in target_pids:
                                found.append((hwnd, val or "Mozilla Firefox", 100))
                            # Match 2: Window class is MozillaWindowClass and title matches stream name / domain
                            elif cls == "MozillaWindowClass":
                                for matcher in [full_netloc, parsed_domain, stream_name]:
                                    if matcher and matcher.lower() in val.lower():
                                        found.append((hwnd, val, 80))
                                        break
                                # Match 3: Visible MozillaWindowClass when no other matches exist
                                if not found and val and not any(skip == val.lower() or skip in val.lower() for skip in _FIREFOX_SKIP):
                                    found.append((hwnd, val, 20))
                    return True

                cb = WNDENUMPROC(_enum_windows)
                user32.EnumWindows(cb, 0)

                if found:
                    found.sort(key=lambda x: x[2], reverse=True)
                    hwnd, title, score = found[0]
                    try:
                        # Disable window resizing (remove WS_THICKFRAME and WS_MAXIMIZEBOX styles)
                        GWL_STYLE = -16
                        WS_THICKFRAME = 0x00040000
                        WS_MAXIMIZEBOX = 0x00010000
                        WS_MINIMIZEBOX = 0x00020000
                        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                        if style:
                            style &= ~(WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX)
                            user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)  # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
                    except Exception as e:
                        logger.debug(f"SetWindowLongW error: {e}")

                    self.window_hwnds[stream_id] = hwnd
                    self.window_titles[stream_id] = title

                    # Ensure Firefox window is visible and anchored at top-left (0,0) for high-performance desktop capture
                    try:
                        is_minimized = user32.IsIconic(hwnd)
                        if is_minimized:
                            SW_RESTORE = 9
                            user32.ShowWindow(hwnd, SW_RESTORE)
                            logger.debug(f"Restored minimized Firefox window (hwnd=0x{hwnd:x})")
                        else:
                            SW_SHOW = 5
                            user32.ShowWindow(hwnd, SW_SHOW)

                        # Position window at (0,0): 1920x1118 for 1080p, 1280x758 for 720p (+38px window titlebar)
                        is_1080p = "1080" in str(resolution or "").lower()
                        win_w = 1920 if is_1080p else 1280
                        win_h = 1118 if is_1080p else 758
                        SWP_SHOWWINDOW = 0x0040
                        user32.SetWindowPos(hwnd, 0, 0, 0, win_w, win_h, SWP_SHOWWINDOW)
                    except Exception as e:
                        logger.debug(f"SetWindowPos/Style error: {e}")

                    logger.info(f"Locked Firefox window (hwnd=0x{hwnd:x}) with title '{title}' for stream '{stream_name}' ({stream_id}) [confidence={score}]")
                    return title

            time.sleep(0.5)

        fallback = full_netloc if full_netloc else (parsed_domain if parsed_domain else stream_name)
        logger.info(f"Window title poll finished; using GDIGrab fallback: '{fallback}'")
        self.window_titles[stream_id] = fallback
        return fallback

    def get_window_hwnd(self, stream_id: str, stream_name: str = "", url: str = "", resolution: str = "720p") -> Optional[int]:
        """Return HWND (int) for stream_id."""
        if stream_id in self.window_hwnds:
            return self.window_hwnds[stream_id]
        self.wait_for_window_title(stream_id, stream_name, url, timeout=10.0, resolution=resolution)
        return self.window_hwnds.get(stream_id)

    def get_window_title(self, stream_id: str, default_name: str = "", url: str = "") -> str:
        """Get expected window title for GDIGrab window capture."""
        if stream_id in self.window_titles:
            return self.window_titles[stream_id]
        return self.wait_for_window_title(stream_id, default_name, url, timeout=5.0)

    def get_live_window_title(self, stream_id: str) -> Optional[str]:
        """Fetch the actual current window title from the OS."""
        hwnd = self.window_hwnds.get(stream_id)
        if not hwnd:
            return None
        if os.name == "nt":
            import ctypes
            user32 = ctypes.windll.user32
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return None
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
        return None

    def close_browser(self, stream_id: str):
        """Clean up state and close browser process tree for stream_id."""
        proc = self.browser_processes.pop(stream_id, None)
        self.window_titles.pop(stream_id, None)
        hwnd = self.window_hwnds.pop(stream_id, None)
        logger.info(f"close_browser: proc={proc is not None}, hwnd={hwnd}")

        pids_to_kill = get_stream_pids(stream_id, proc.pid if proc else None)

        if os.name == "nt" and hwnd:
            try:
                from ctypes import wintypes
                main_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(main_pid))
                if main_pid.value:
                    pids_to_kill.add(main_pid.value)
            except Exception as e:
                logger.debug(f"Error reading HWND PID: {e}")

            # Try graceful window close first
            try:
                WM_CLOSE = 0x0010
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

        for pid in pids_to_kill:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=3,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            except Exception:
                pass

    def purge_all(self):
        """Purge all browser processes and temporary profiles in BROWSER_PROFILES_DIR."""
        for stream_id in list(self.browser_processes.keys()):
            self.close_browser(stream_id)

        self.window_titles.clear()
        self.window_hwnds.clear()
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
