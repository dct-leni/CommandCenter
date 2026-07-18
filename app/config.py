"""
Configuration loader and saver for CommandCenter.
Reads/writes config.yml with dataclass-based defaults.
"""

import os
import yaml
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List

CONFIG_PATH = Path(__file__).parent.parent / "config.yml"


@dataclass
class ConverterConfig:
    source_folder: str = "input/"
    # Audio languages to keep (ISO 639-2/1 tags, or anything matched against
    # stream language tags / titles). First match wins in the order listed;
    # if none of these are found, the converter falls back to the first
    # audio track present.
    languages: List[str] = field(default_factory=lambda: ["tur", "tr", "trk"])


@dataclass
class StreamerConfig:
    content_folder: str = "streams/"
    port_range_start: int = 1935
    port_range_end: int = 1944
    protocol: str = "rtmp"  # rtmp or hls
    auto_resume: bool = False
    current_folder: str = ""
    channel_prefix: str = "Salon"       # Channel name prefix (e.g. "Salon" → "Salon1 HD")
    epg_timezone: str = "+0300"         # XMLTV timezone offset string
    playlists: dict = field(default_factory=dict)  # folder_name → [{port, files}]


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class AppConfig:
    converter: ConverterConfig = field(default_factory=ConverterConfig)
    streamer: StreamerConfig = field(default_factory=StreamerConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def load_config() -> AppConfig:
    """Load configuration from config.yml, creating defaults if missing."""
    if not CONFIG_PATH.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    converter_data = data.get("converter", {})
    streamer_data = data.get("streamer", {})
    server_data = data.get("server", {})

    return AppConfig(
        converter=ConverterConfig(**{k: v for k, v in converter_data.items() if k in ConverterConfig.__dataclass_fields__}),
        streamer=StreamerConfig(**{k: v for k, v in streamer_data.items() if k in StreamerConfig.__dataclass_fields__}),
        server=ServerConfig(**{k: v for k, v in server_data.items() if k in ServerConfig.__dataclass_fields__}),
    )


def save_config(cfg: AppConfig) -> None:
    """Save configuration to config.yml."""
    data = asdict(cfg)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def update_config(updates: dict) -> AppConfig:
    """Merge partial updates into existing config and save."""
    cfg = load_config()

    if "converter" in updates:
        for k, v in updates["converter"].items():
            if hasattr(cfg.converter, k):
                setattr(cfg.converter, k, v)

    if "streamer" in updates:
        for k, v in updates["streamer"].items():
            if hasattr(cfg.streamer, k):
                setattr(cfg.streamer, k, v)

    if "server" in updates:
        for k, v in updates["server"].items():
            if hasattr(cfg.server, k):
                setattr(cfg.server, k, v)

    save_config(cfg)
    return cfg