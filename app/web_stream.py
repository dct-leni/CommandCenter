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

    # Dynamic fallback: search anywhere inside bin/ for phyrox-portable.exe first, then firefox.exe
    bin_dir = _BASE_DIR / "bin"
    if bin_dir.exists():
        for found in bin_dir.rglob("phyrox-portable.exe"):
            logger.info(f"Found phyrox-portable.exe via rglob scan at: {found}")
            return str(found)
        for found in bin_dir.rglob("firefox.exe"):
            logger.info(f"Found firefox.exe via rglob scan at: {found}")
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

        # Write to app/distribution, data, and parent/distribution
        dirs_to_try = [
            firefox_exe.parent / "distribution",
            firefox_exe.parent.parent / "distribution",
            firefox_exe.parent / "app" / "distribution",  # For phyrox-portable.exe -> app/distribution
            firefox_exe.parent / "data",                  # Portapps requires data/policies.json
        ]
        for d in dirs_to_try:
            try:
                d.mkdir(exist_ok=True)
                (d / "policies.json").write_text(policy_json, encoding="utf-8")
            except Exception:
                pass
        logger.info("Created Enterprise policies.json to bypass Firefox Welcome / Terms of Use prompts.")
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
        "addonStartup.json.lz4"
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

    # --- Generate Page Visibility Override Extension ---
    # This prevents sites like YouTube/Twitch/TikTok from pausing video when unfocused
    ext_dir = profile_dir / "extensions"
    ext_dir.mkdir(exist_ok=True)
    ext_id = "visibility@commandcenter.local"
    ext_path = ext_dir / f"{ext_id}.xpi"
    
    manifest = {
        "manifest_version": 2,
        "name": "Visibility Override",
        "version": "1.0",
        "browser_specific_settings": {
            "gecko": {
                "id": ext_id
            }
        },
        "content_scripts": [{
            "matches": ["<all_urls>"],
            "js": ["content.js"],
            "run_at": "document_start",
            "all_frames": True
        }],
        "permissions": [
            "http://127.0.0.1/*",
            "http://localhost/*"
        ]
    }
    content_js = r"""
    (function() {
        // 1. Override Page Visibility API (Bypasses auto-pause on background tabs / unfocused window)
        try {
            Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
            Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
            window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
        } catch(e) {}

        // 2. Video Playback Detection with 4-second delay
        let playSeconds = 0;
        let idleTimer = null;

        function checkVideo() {
            const videos = Array.from(document.querySelectorAll('video'));
            let isPlaying = false;

            for (const v of videos) {
                const rect = v.getBoundingClientRect();
                if (rect.width > 200 && rect.height > 150 && !v.paused && !v.ended && v.readyState > 2) {
                    isPlaying = true;
                    break;
                }
            }

            if (isPlaying) {
                playSeconds += 1;
            } else {
                playSeconds = 0;
            }

            // Only auto-hide headers after 4 seconds of confirmed continuous playback
            if (playSeconds >= 4 && document.body) {
                document.body.classList.add('cc-hide-nav');
            } else if (document.body) {
                document.body.classList.remove('cc-hide-nav');
            }
        }

        // 3. Mouse Activity: Reveal headers immediately on mouse movement
        function handleUserActivity() {
            if (document.body && document.body.classList.contains('cc-hide-nav')) {
                document.body.classList.remove('cc-hide-nav');
            }
            if (idleTimer) clearTimeout(idleTimer);
            idleTimer = setTimeout(() => {
                if (playSeconds >= 4 && document.body) {
                    document.body.classList.add('cc-hide-nav');
                }
            }, 3500);
        }

        window.addEventListener('mousemove', handleUserActivity, { passive: true });
        window.addEventListener('mousedown', handleUserActivity, { passive: true });
        window.addEventListener('keydown', handleUserActivity, { passive: true });

        // 4. Auto-accept First Run / Onboarding Dialogs
        function autoAcceptPrompts() {
            document.querySelectorAll('button, a').forEach(b => {
                const txt = (b.textContent || '').trim().toLowerCase();
                if (txt === 'continue' || txt === 'agree' || txt === 'accept' || txt === 'get started') {
                    if (document.body && (document.body.textContent.includes('Welcome to Firefox') || document.body.textContent.includes('Terms of Use'))) {
                        try { b.click(); } catch(e) {}
                    }
                }
            });
        }

        setInterval(checkVideo, 1000);
        setInterval(autoAcceptPrompts, 2000);
    })();
    """
    
    with zipfile.ZipFile(ext_path, 'w') as zf:
        zf.writestr('manifest.json', json.dumps(manifest))
        zf.writestr('content.js', content_js.replace("{stream_id}", stream_id))

    # --- Write user.js preferences ---
    user_js = profile_dir / "user.js"
    prefs = [
        # --- Media autoplay & audio sink selection always allowed ---
        'user_pref("media.autoplay.default", 0);',
        'user_pref("media.autoplay.blocking_policy", 0);',
        'user_pref("media.autoplay.allow-muted", true);',
        'user_pref("media.autoplay.enabled.user-gestures-needed", false);',
        'user_pref("permissions.default.autoplay-media", 1);',

        # --- Disable First Run / Welcome / Onboarding Pages ---
        'user_pref("browser.aboutwelcome.enabled", false);',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");',
        'user_pref("startup.homepage_welcome_url", "");',
        'user_pref("startup.homepage_welcome_url.additional", "");',
        'user_pref("browser.onboarding.enabled", false);',
        'user_pref("browser.onboarding.hidden", true);',
        'user_pref("browser.onboarding.notification.finished", true);',
        'user_pref("browser.uitour.enabled", false);',
        'user_pref("datareporting.policy.dataSubmissionPolicyAcceptedVersion", 999);',
        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);',
        'user_pref("datareporting.policy.firstRunURL", "");',
        'user_pref("browser.rights.3.shown", true);',
        'user_pref("browser.rights.override", "show");',
        'user_pref("browser.rights.silence", true);',
        'user_pref("browser.tos.accepted", true);',
        'user_pref("browser.tos.shown", true);',
        'user_pref("browser.newtabpage.introShown", true);',
        'user_pref("trailhead.firstrun.didSeeAboutWelcome", true);',

        # --- Enable unsigned extensions & stylesheet customization ---
        'user_pref("xpinstall.signatures.required", false);',
        'user_pref("extensions.experiments.enabled", true);',
        'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);',

        # --- Optimize for background rendering and GDI capture ---
        'user_pref("dom.suspend_inactive.enabled", false);',
        'user_pref("dom.timeout.enable_budget_timer_throttling", false);',
        'user_pref("widget.windows.window_occlusion_tracking.enabled", false);',
        'user_pref("dom.ipc.processPriorityManager.backgroundUsesEcoQoS", false);',
        'user_pref("network.http.throttle.enable", false);',
        'user_pref("media.block-autoplay-until-in-foreground", false);',

        # --- Force Software WebRender (Direct GDI window compatibility) ---
        'user_pref("gfx.webrender.all", false);',
        'user_pref("gfx.webrender.software", true);',
        'user_pref("layers.acceleration.disabled", true);',

        # --- Cookie / Banner handling ---
        'user_pref("cookiebanners.service.mode", 2);',
        'user_pref("cookiebanners.service.mode.privateBrowsing", 2);',
        'user_pref("privacy.donottrackheader.enabled", true);',

        # --- Disable popups & notifications ---
        'user_pref("dom.disable_open_during_load", true);',
        'user_pref("permissions.default.desktop-notification", 2);',
        'user_pref("dom.webnotifications.enabled", false);',

        # --- Disable crash prompts ---
        'user_pref("browser.crashReports.unsubmittedCheck.enabled", false);',
        'user_pref("browser.crashReports.unsubmittedCheck.autoSubmit2", false);',
        'user_pref("media.eme.enabled", true);',
        'user_pref("media.gmp-widevinecdm.enabled", true);',
        'user_pref("media.gmp-widevinecdm.visible", true);',

        # --- WebRTC / Network Prefs ---
        'user_pref("media.peerconnection.enabled", false);',  # Disable WebRTC (prevents STUN/TURN IP leaks)
        'user_pref("intl.accept_languages", "tr-TR, tr, en-US, en");',
        'user_pref("javascript.use_us_english_locale", false);',

        # --- Force Reliable TCP over SOCKS5 (Fixes Secure Connection Failed) ---
        'user_pref("network.http.http3.enable", false);',  # Disable QUIC/UDP (prevents SOCKS5 UDP Associate failure)
        'user_pref("network.http.spdy.enabled.http2", true);',
        'user_pref("network.dns.disableIPv6", true);',  # Disable IPv6 (SOCKS5 tunnels often fail IPv6)
    ]

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
                    f'user_pref("network.proxy.http", "{host}");',
                    f'user_pref("network.proxy.http_port", {port});',
                    f'user_pref("network.proxy.ssl", "{host}");',
                    f'user_pref("network.proxy.ssl_port", {port});',
                    'user_pref("network.proxy.type", 1);',
                ])
            logger.info(f"Applied Firefox proxy settings for {proxy_url}")
        except Exception as e:
            logger.warning(f"Failed to parse proxy_url {proxy_url}: {e}")
    else:
        prefs.append('user_pref("network.proxy.type", 0);')

    user_js.write_text("\n".join(prefs), encoding="utf-8")

    # --- Write userChrome.css to collapse address bar, tabs, and navigation toolbars ---
    chrome_dir = profile_dir / "chrome"
    chrome_dir.mkdir(exist_ok=True)
    
    (chrome_dir / "userChrome.css").write_text(
        "#nav-bar, #TabsToolbar, #PersonalToolbar, #sidebar-box, #toolbar-menubar {\n"
        "    visibility: collapse !important;\n"
        "}\n",
        encoding="utf-8"
    )

    # --- Write userContent.css to natively manage video player full-window & hover UI ---
    (chrome_dir / "userContent.css").write_text(
        "/* Hide Radiant Media Player ad overlays and Google IMA iframe containers */\n"
        ".rmp-ad-container, .ima-ad-container, [id*='google_ads_iframe'] {\n"
        "    display: none !important;\n"
        "    opacity: 0 !important;\n"
        "    pointer-events: none !important;\n"
        "    width: 0 !important;\n"
        "    height: 0 !important;\n"
        "}\n"
        "\n"
        "/* Eliminate the 96px top margin on Exxen's main content wrapper to remove top black bar */\n"
        "div[style*=\"margin-top: 96px\"] {\n"
        "    margin-top: 0 !important;\n"
        "    min-height: 100vh !important;\n"
        "}\n"
        "\n"
        "/* Ensure the body remains black and hides scrollbars visually without blocking scrolling */\n"
        "html, body {\n"
        "    background: #000 !important;\n"
        "    scrollbar-width: none !important;\n"
        "    -ms-overflow-style: none !important;\n"
        "}\n"
        "html::-webkit-scrollbar, body::-webkit-scrollbar {\n"
        "    display: none !important;\n"
        "}\n"
        "\n"
        "/* Make the video player fill the entire browser window */\n"
        "#full-screen-closed {\n"
        "    position: fixed !important;\n"
        "    top: 0 !important;\n"
        "    left: 0 !important;\n"
        "    width: 100vw !important;\n"
        "    height: 100vh !important;\n"
        "    z-index: 99998 !important;\n"
        "    background: #000 !important;\n"
        "}\n"
        "#er-player, #zon-player-parent, #video-player, #radiant-player, .rmp-content {\n"
        "    width: 100% !important;\n"
        "    height: 100% !important;\n"
        "}\n"
        ".rmp-video, video.rmp-video {\n"
        "    width: 100% !important;\n"
        "    height: 100% !important;\n"
        "    object-fit: contain !important;\n"
        "    background: #000 !important;\n"
        "}\n"
        "\n"
        "/* Site header stays above the video player so hovering top reveals it */\n"
        "header, nav, [class*='header'], [class*='nav'], [class*='menu'] {\n"
        "    z-index: 100001 !important;\n"
        "}\n"
        "\n"
        "/* Auto-hide headers and footers natively, reveal on mouse hover */\n"
        "header, nav, footer, aside, #footer-id, #header-id,\n"
        "[id*='footer'], [class*='footer'], [id*='Footer'], [class*='Footer'],\n"
        "[id*='header'], [class*='header'], [id*='Header'], [class*='Header'],\n"
        "[id*='nav'], [class*='nav'], [id*='Nav'], [class*='Nav'],\n"
        "[class*='menu'], [class*='Menu'] {\n"
        "    opacity: 0 !important;\n"
        "    transition: opacity 0.3s ease-in-out !important;\n"
        "}\n"
        "\n"
        "header:hover, nav:hover, footer:hover, aside:hover, #footer-id:hover, #header-id:hover,\n"
        "[id*='footer']:hover, [class*='footer']:hover, [id*='Footer']:hover, [class*='Footer']:hover,\n"
        "[id*='header']:hover, [class*='header']:hover, [id*='Header']:hover, [class*='Header']:hover,\n"
        "[id*='nav']:hover, [class*='nav']:hover, [id*='Nav']:hover, [class*='Nav']:hover,\n"
        "[class*='menu']:hover, [class*='Menu']:hover {\n"
        "    opacity: 1 !important;\n"
        "    pointer-events: auto !important;\n"
        "}\n",
        encoding="utf-8"
    )


class WebStreamManager:
    def __init__(self):
        self.browser_processes: Dict[str, subprocess.Popen] = {}
        self.window_titles: Dict[str, str] = {}
        self.window_hwnds: Dict[str, int] = {}  # HWND of the Firefox content window

    def launch_browser(self, stream_id: str, name: str, url: str, proxy_url: Optional[str] = None) -> str:
        """
        Launch a 1280x720 Portable Firefox popup for a web stream.
        Creates an isolated profile with the CommandCenter MV2 audio extension pre-loaded.
        """
        self.close_browser(stream_id)

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
                        "--width=1280",
                        "--height=720",
                        url,
                        "-foreground"
                    ]
                    env = os.environ.copy()
                    env["TZ"] = "Europe/Istanbul"
                    logger.info(f"Launching Portapps phyrox-portable for '{name}' ({stream_id}) -> '{url}'")
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
                    "--width=1280",
                    "--height=720",
                    url,
                    "-foreground"
                ]
                env = os.environ.copy()
                env["TZ"] = "Europe/Istanbul"
                logger.info(f"Launching Native Firefox for web stream '{name}' ({stream_id}) -> '{url}'")
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

    def wait_for_window_title(self, stream_id: str, stream_name: str, url: str, timeout: float = 10.0) -> str:
        """Poll window titles for up to timeout seconds. Also stores the HWND for region-based capture."""
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

        _FIREFOX_SKIP = {
            "firefox media keys",
            "about:blank", "new tab", "before you continue",
            "privacy policy", "cookie", "consent",
        }

        while time.time() - start_time < timeout:
            if proc and proc.pid:
                target_pids = get_child_pids(proc.pid)
            # 1. Scan PIDs — capture both title and HWND
            if target_pids and os.name == "nt":
                found: List[tuple] = []  # (hwnd, title)

                def _enum_pid_windows(hwnd, lParam):
                    if user32.IsWindowVisible(hwnd):
                        pid = ctypes.wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value in target_pids:
                            buff = ctypes.create_unicode_buffer(512)
                            user32.GetWindowTextW(hwnd, buff, 512)
                            val = buff.value.strip()
                            if val and not any(skip == val.lower() or skip in val.lower() for skip in _FIREFOX_SKIP):
                                # Verify this is a main window (not a tiny helper window)
                                rect = ctypes.wintypes.RECT()
                                user32.GetClientRect(hwnd, ctypes.byref(rect))
                                w = rect.right - rect.left
                                h = rect.bottom - rect.top
                                if w >= 400 and h >= 300:
                                    found.append((hwnd, val))
                    return True

                cb = WNDENUMPROC(_enum_pid_windows)
                user32.EnumWindows(cb, 0)
                if found:
                    hwnd, title = found[0]
                    try:
                        # Disable window resizing (remove WS_THICKFRAME and WS_MAXIMIZEBOX styles)
                        GWL_STYLE = -16
                        WS_THICKFRAME = 0x00040000
                        WS_MAXIMIZEBOX = 0x00010000
                        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                        if style:
                            style &= ~(WS_THICKFRAME | WS_MAXIMIZEBOX)
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
                        
                        # Position window at (0,0) 1280x758
                        SWP_SHOWWINDOW = 0x0040
                        user32.SetWindowPos(hwnd, 0, 0, 0, 1280, 758, SWP_SHOWWINDOW)
                        
                        # Remove Minimize Box to prevent GDI capture freezing
                        GWL_STYLE = -16
                        WS_MINIMIZEBOX = 0x00020000
                        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                        if style & WS_MINIMIZEBOX:
                            user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_MINIMIZEBOX)
                    except Exception as e:
                        logger.debug(f"SetWindowPos/Style error: {e}")

                    logger.info(f"Locked Firefox window title to '{title}' for stream '{stream_name}' ({stream_id}) (hwnd=0x{hwnd:x})")
                    return title
                else:
                    logger.debug(f"PID scan target_pids={target_pids} found 0 matching windows so far...")

            # 2. Fallback: search open system window titles and lock HWND (PID linkage broken by launcher)
            if os.name == "nt":
                found_fallback: List[tuple] = []
                def _enum_title_windows(hwnd, lParam):
                    if user32.IsWindowVisible(hwnd):
                        buff = ctypes.create_unicode_buffer(512)
                        user32.GetWindowTextW(hwnd, buff, 512)
                        val = buff.value.strip()
                        if val and not any(skip == val.lower() or skip in val.lower() for skip in _FIREFOX_SKIP):
                            rect = ctypes.wintypes.RECT()
                            user32.GetClientRect(hwnd, ctypes.byref(rect))
                            if (rect.right - rect.left) >= 400 and (rect.bottom - rect.top) >= 300:
                                found_fallback.append((hwnd, val))
                    return True
                
                cb_title = WNDENUMPROC(_enum_title_windows)
                user32.EnumWindows(cb_title, 0)
                
                for hwnd, t in found_fallback:
                    for matcher in [full_netloc, parsed_domain, stream_name]:
                        if matcher and matcher.lower() in t.lower():
                            logger.info(f"Detected open Firefox window for stream '{stream_name}' matching '{matcher}': '{t}' (hwnd=0x{hwnd:x})")
                            self.window_hwnds[stream_id] = hwnd
                            self.window_titles[stream_id] = t
                            try:
                                GWL_STYLE = -16
                                WS_THICKFRAME = 0x00040000
                                WS_MAXIMIZEBOX = 0x00010000
                                WS_MINIMIZEBOX = 0x00020000
                                style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                                if style:
                                    style &= ~(WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX)
                                    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                                if user32.IsIconic(hwnd):
                                    user32.ShowWindow(hwnd, 9)
                                else:
                                    user32.ShowWindow(hwnd, 5)
                                user32.SetWindowPos(hwnd, 0, 0, 0, 1280, 758, 0x0067)
                            except Exception as e:
                                logger.debug(f"Fallback window setup error: {e}")
                            return t
            time.sleep(0.5)

        fallback = full_netloc if full_netloc else (parsed_domain if parsed_domain else stream_name)
        logger.info(f"Window title poll finished; using GDIGrab fallback: '{fallback}'")
        self.window_titles[stream_id] = fallback
        return fallback

    def get_window_hwnd(self, stream_id: str, stream_name: str = "", url: str = "") -> Optional[int]:
        """Return HWND (int) for stream_id."""
        if stream_id in self.window_hwnds:
            return self.window_hwnds[stream_id]
        self.wait_for_window_title(stream_id, stream_name, url, timeout=10.0)
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

        pids_to_kill = set()
        if os.name == "nt" and hwnd:
            try:
                from ctypes import wintypes
                main_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(main_pid))
                if main_pid.value:
                    pids_to_kill.add(main_pid.value)
            except Exception as e:
                logger.debug(f"Error reading HWND PID: {e}")

        if proc:
            pids_to_kill.add(proc.pid)

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
