"""Windows auto-start registration via HKCU Run key.

No external tools: registers a Run-key entry that launches start.bat
(visible console window, same path as a manual start) at user logon.
Toggle from the Web UI; persisted in config.yml as server.auto_start.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("commandcenter")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "CommandCenter"


def _launcher_command() -> str:
    root = Path(__file__).resolve().parent.parent
    return f'cmd /c "{root / "start.bat"}"'


def set_auto_start(enabled: bool) -> bool:
    """Register or remove the Run-key entry. Returns True on success."""
    if os.name != "nt":
        logger.info("Auto-start toggle ignored: not Windows")
        return True
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _launcher_command())
                logger.info(f"Auto-start registered: {_VALUE_NAME}")
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                    logger.info("Auto-start unregistered")
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        logger.error(f"Failed to update auto-start registry entry: {e}")
        return False


def is_auto_start_registered() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
            return True
    except OSError:
        retu         