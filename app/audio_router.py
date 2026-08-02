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


def route_to_vb_cable() -> bool:
    """
    Switch ONLY 'firefox.exe' audio output to CABLE Input using SoundVolumeView.
    Leaves global system default audio untouched on user speakers.
    """
    global _ACTIVE_WEB_STREAMS_COUNT
    _ACTIVE_WEB_STREAMS_COUNT += 1

    svv_exe = get_soundvolumeview_path()
    if not svv_exe.exists():
        logger.warning(f"SoundVolumeView.exe missing at {svv_exe}")
        return False

    try:
        cable_input_name, _ = get_cable_device_ids()
        cable_target = cable_input_name or _CABLE_INPUT_NAME_FALLBACK
        _com_init()
        try:
            from pycaw.pycaw import AudioUtilities
            for device in AudioUtilities.GetAllDevices():
                if "cable input" in (device.FriendlyName or "").lower() and device.id:
                    cable_target = device.id
                    break
        except Exception:
            pass
        finally:
            _com_uninit()

        # Route firefox.exe specifically to CABLE Input across all roles (Console=0, Multimedia=1, Communications=2)
        # SoundVolumeView syntax: /SetAppDefault <Device Name/ID> <Role> <Process Name/PID>
        for role in (0, 1, 2):
            subprocess.run(
                [str(svv_exe), "/SetAppDefault", cable_target, str(role), "firefox.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        logger.info(f"Set application-level audio output of 'firefox.exe' to CABLE Input ({cable_target}) via SoundVolumeView")
        return True
    except Exception as e:
        logger.error(f"Failed to set firefox.exe audio device to CABLE Input: {e}")
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
        # Reset per-app routing for firefox.exe
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
