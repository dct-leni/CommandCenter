"""
app/audio_router.py — Virtual Audio Router for CommandCenter Web Streams.

Routes ONLY 'firefox.exe' audio output to CABLE Input using SoundVolumeView.exe during web streams,
leaving system default audio untouched on user speakers.
Restores firefox.exe audio output to Default when all web streams end.
"""

import os
import subprocess
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_CABLE_INPUT_NAME_FALLBACK = "CABLE Input (VB-Audio Virtual Cable)"
_CABLE_OUTPUT_NAME_FALLBACK = "CABLE Output (VB-Audio Virtual Cable)"

_ACTIVE_WEB_STREAMS_COUNT = 0


def _com_init():
    """Initialize COM on current thread."""
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def _com_uninit():
    """Uninitialize COM on current thread."""
    try:
        import comtypes
        comtypes.CoUninitialize()
    except Exception:
        pass


def get_soundvolumeview_path() -> Path:
    """Return path to bin/SoundVolumeView.exe."""
    base_dir = Path(__file__).resolve().parent.parent
    return base_dir / "bin" / "SoundVolumeView.exe"

def get_cable_device_ids() -> Tuple[Optional[str], Optional[str]]:
    """Return (cable_input_name, cable_output_name) friendly device names."""
    cable_input_name = None
    cable_output_name = None
    _com_init()
    try:
        from pycaw.pycaw import AudioUtilities
        for device in AudioUtilities.GetAllDevices():
            fname = device.FriendlyName or ""
            if "cable input" in fname.lower() and not cable_input_name:
                cable_input_name = fname
            elif "cable output" in fname.lower() and not cable_output_name:
                cable_output_name = fname
    except Exception:
        pass
    finally:
        _com_uninit()

    return (
        cable_input_name or _CABLE_INPUT_NAME_FALLBACK,
        cable_output_name or _CABLE_OUTPUT_NAME_FALLBACK,
    )


import asyncio

def route_to_vb_cable() -> bool:
    """
    Switch ONLY 'firefox.exe' audio output to CABLE Input using SoundVolumeView.
    This runs asynchronously and polls until the Firefox audio session is initialized.
    Leaves global system default audio untouched on user speakers.
    """
    global _ACTIVE_WEB_STREAMS_COUNT
    _ACTIVE_WEB_STREAMS_COUNT += 1

    svv_exe = get_soundvolumeview_path()
    if not svv_exe.exists():
        logger.warning(f"SoundVolumeView.exe missing at {svv_exe}")
        return False

    # Launch background polling task
    async def _poll_and_route():
        logger.info("Polling SoundVolumeView to dynamically resolve CABLE Input and route firefox.exe ...")
        for _ in range(60):  # Poll for up to 30 seconds
            try:
                # Dump current audio sessions
                temp_json = _BASE_DIR / "temp" / "svv.json"
                temp_json.parent.mkdir(parents=True, exist_ok=True)
                
                subprocess.run(
                    [str(svv_exe), "/sjson", str(temp_json)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                
                import json
                if temp_json.exists():
                    with open(temp_json, "r", encoding="utf-16") as f:
                        data = json.load(f)
                    
                    # 1. Dynamically find the VB-Cable Input Item ID on this specific PC
                    cable_target_id = None
                    for item in data:
                        name = (item.get("Name") or "").lower()
                        if "cable input" in name:
                            # Use Item ID if available, otherwise Command-Line Friendly ID
                            cable_target_id = item.get("Item ID") or item.get("Command-Line Friendly ID")
                            break
                            
                    if not cable_target_id:
                        # Fallback if somehow not listed
                        cable_target_id = "CABLE Input (VB-Audio Virtual Cable)"
                        
                    # 2. Find Firefox and route it
                    found = False
                    for item in data:
                        proc_path = item.get("Process Path", "")
                        if proc_path and "firefox.exe" in proc_path.lower():
                            # Found the audio session! Route it explicitly.
                            proc_id = str(item.get("Process ID", "firefox.exe"))
                            for role in (0, 1, 2):
                                subprocess.run(
                                    [str(svv_exe), "/SetAppDefault", cable_target_id, str(role), proc_id],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                )
                            found = True
                            
                    if found:
                        logger.info(f"Successfully routed Firefox audio session to VB-Cable ID: {cable_target_id}")
                        return
            except Exception as e:
                logger.debug(f"Error during audio routing poll: {e}")
                
            await asyncio.sleep(0.5)
            
        logger.warning("Timed out waiting for Firefox audio session to appear in SoundVolumeView.")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_poll_and_route())
        return True
    except RuntimeError:
        return False


def restore_audio_routing() -> None:
    """Reset firefox.exe application-level audio output back to Default when all web streams stop."""
    global _ACTIVE_WEB_STREAMS_COUNT
    if _ACTIVE_WEB_STREAMS_COUNT > 0:
        _ACTIVE_WEB_STREAMS_COUNT -= 1

    if _ACTIVE_WEB_STREAMS_COUNT > 0:
        logger.info(f"Web streams still active ({_ACTIVE_WEB_STREAMS_COUNT}), keeping firefox.exe audio routing")
        return

    svv_exe = get_soundvolumeview_path()
    if not svv_exe.exists():
        return

    try:
        # Reset per-app routing for all firefox.exe processes
        for role in (0, 1, 2):
            subprocess.run(
                [str(svv_exe), "/SetAppDefault", "", str(role), "firefox.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        logger.info("Reset application-level audio output of 'firefox.exe' to Default via SoundVolumeView")
    except Exception as e:
        logger.error(f"Error resetting firefox.exe audio routing: {e}")
