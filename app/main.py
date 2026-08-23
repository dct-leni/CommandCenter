"""
CommandCenter — FastAPI application entry point.
Serves the Web UI and provides REST API for converter and streamer.

Endpoints live in app/routers/ (converter, streamer, live, system).
"""

import logging
import os
import signal
import atexit
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.config import load_config
from app.ffmpeg_setup import get_binaries_status
from app.converter import converter
from app.streamer import streamer
from app.live_relay import live_relay_manager
from app.routers import converter as converter_routes
from app.routers import streamer as streamer_routes
from app.routers import live as live_routes
from app.routers import system as system_routes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("commandcenter")

def safe_create_task(coro, name=None):
    """Create a background task with automatic exception logging on completion."""
    task = asyncio.create_task(coro, name=name)
    def _handle_result(t):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Unhandled exception in background task '{name or getattr(coro, '__name__', str(coro))}': {e}", exc_info=True)
    task.add_done_callback(_handle_result)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Set Windows system timer resolution to 1ms (eliminates 15.6ms OS timer interrupt quantum slips)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
            logger.info("Set Windows system timer resolution to 1ms (timeBeginPeriod(1))")
        except Exception as e:
            logger.warning(f"Failed to set Windows timer resolution: {e}")

    # Silence harmless Windows Proactor connection-reset exceptions (WinError 10054)
    loop = asyncio.get_running_loop()
    def custom_exception_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) or (isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10054):
            return
        loop.default_exception_handler(context)
    loop.set_exception_handler(custom_exception_handler)

    from app.vpn_manager import vpn_manager
    from app.web_stream import web_stream_manager
    vpn_manager.purge_temp_dir()
    web_stream_manager.purge_all()
    # Resolve external IP on app start
    safe_create_task(streamer._resolve_external_ip(), name="resolve_external_ip")
    # Start Global VPN on app startup if configured
    vpn_manager.start_global_vpn()

    # Populate configured folders and handle auto-resume on boot
    cfg = load_config()
    if cfg.streamer.content_folder and Path(cfg.streamer.content_folder).is_dir():
        streamer.scan_content_folder(cfg.streamer.content_folder)
        if cfg.streamer.auto_resume:
            logger.info(f"Auto-resume enabled, starting stream for folder: {cfg.streamer.content_folder}")
            safe_create_task(
                streamer.start_streaming(cfg.streamer.port_range_start, cfg.streamer.port_range_end),
                name="auto_resume_folder_streaming"
            )
        
    # Resume auto_start live streams across server restarts (excluding web streams)
    for ls_item in cfg.streamer.live_streams:
        if ls_item.get("auto_start"):
            try:
                logger.info(f"Auto-resuming live relay stream: {ls_item.get('name')} on :{ls_item.get('port')}")
                safe_create_task(
                    live_relay_manager.start_stream(ls_item.get("id")),
                    name=f"auto_start_relay_{ls_item.get('id')}"
                )
            except Exception as e:
                logger.error(f"Failed to auto-resume live stream {ls_item.get('id')}: {e}")

    yield

    if streamer.is_running:
        # Pass is_shutdown=True to prevent wiping auto_resume state on restart
        await streamer.stop_streaming(is_shutdown=True)

    for ls_status in list(live_relay_manager.active_relays.values()):
        await live_relay_manager.stop_stream(ls_status.id)

    vpn_manager.purge_temp_dir()
    web_stream_manager.purge_all()

# Create FastAPI app
app = FastAPI(title="CommandCenter", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def lan_only_guard(request: Request, call_next):
    # Allow loopback + private/LAN ranges only (127.*, ::1, 192.168.*, 10.*,
    # 172.16-31.*, link-local). Public internet addresses are rejected.
    import ipaddress
    try:
        addr = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not (addr.is_loopback or addr.is_private or addr.is_link_local):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await call_next(request)

# Static files (Web UI)
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# API routers
app.include_router(system_routes.router)
app.include_router(converter_routes.router)
app.include_router(streamer_routes.router)
app.include_router(live_routes.router)


# ──────────────────────────────────────────────
#  Root — serve index.html
# ──────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────

def _kill_all_children():
    """
    Kill the entire process tree of this Python process (FFmpeg, MediaMTX, VPN proxies, etc.).
    Uses Windows-native `taskkill /F /T` for recursive termination.
    Called by both the console-close handler and atexit so no orphans survive.
    """
    try:
        from app.vpn_manager import vpn_manager
        vpn_manager.stop_all()
    except Exception:
        pass
    if os.name == "nt":
        import subprocess as _sp
        try:
            _sp.run(
                ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                creationflags=_sp.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    else:
        import subprocess as _sp
        try:
            _sp.run(["kill", "-TERM", f"-{os.getpid()}"], stderr=_sp.DEVNULL)
        except Exception:
            pass


def main():
    cfg = load_config()
    binaries = get_binaries_status()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║         ⚡ CommandCenter v1.0            ║")
    print("  ║    Video Converter & RTMP Streamer       ║")
    print("  ╚══════════════════════════════════════════╝")
    print()
    print(f"  FFmpeg:   {'✓ Ready' if binaries['ffmpeg'] else '✗ Missing — run setup_binaries.bat'}")
    print(f"  MediaMTX: {'✓ Ready' if binaries['mediamtx'] else '✗ Missing — run setup_binaries.bat'}")
    print(f"  Encoder:  {binaries['best_encoder']} (Auto-detected)")
    print(f"  Web UI:   http://localhost:{cfg.server.port}")
    print()

    # ── Windows console-close handler ─────────────────────────────────────
    # When the user clicks X on the console window (or logs off / shuts down),
    # Windows sends CTRL_CLOSE_EVENT. Uvicorn only handles SIGINT (Ctrl+C), so
    # the window-close leaves FFmpeg / MediaMTX orphaned.
    # We register a handler that kills the full process tree immediately.
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes
        import subprocess as _sp

        CTRL_CLOSE_EVENT    = 2
        CTRL_LOGOFF_EVENT   = 5
        CTRL_SHUTDOWN_EVENT = 6

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
        def _ctrl_handler(ctrl_type):
            if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                logger.info("Console close/logoff event — killing process tree")
                try:
                    _sp.run(
                        ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                        stdout=_sp.DEVNULL,
                        stderr=_sp.DEVNULL,
                        creationflags=_sp.CREATE_NO_WINDOW,
                    )
                except Exception:
                    pass
                return True  # suppress Windows' own delayed hard-kill
            return False  # let Ctrl+C / Ctrl+Break reach uvicorn normally

        ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler, True)
        logger.info("Registered Windows console-close handler")

    # ── atexit fallback ───────────────────────────────────────────────────
    # Runs on any clean Python exit (SIGINT → uvicorn shutdown).
    # Catches any surviving child processes lifespan teardown may have missed.
    atexit.register(_kill_all_children)

    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        use_colors=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
