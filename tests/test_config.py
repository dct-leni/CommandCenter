"""Config load/save/update tests (run against a temp config.yml via monkeypatch)."""
import yaml

import app.config as cfg_mod
from app.config import AppConfig, load_config, save_config, update_config


def use_tmp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.yml")
    return tmp_path / "config.yml"


def test_defaults_created_when_missing(monkeypatch, tmp_path):
    path = use_tmp_config(monkeypatch, tmp_path)
    cfg = load_config()
    assert path.exists()
    assert cfg.server.port == 8080
    assert cfg.streamer.port_range_start == 1935


def test_update_merges_partial_sections(monkeypatch, tmp_path):
    path = use_tmp_config(monkeypatch, tmp_path)
    update_config({"server": {"port": 9090}})
    cfg = load_config()
    assert cfg.server.port == 9090
    # unrelated fields preserved
    assert cfg.streamer.port_range_start == 1935


def test_save_is_idempotent(monkeypatch, tmp_path):
    path = use_tmp_config(monkeypatch, tmp_path)
    save_config(load_config())
    first = path.read_text(encoding="utf-8")
    save_config(load_config())
    assert path.read_text(encoding="utf-8") == first


def test_unknown_keys_dropped_on_load(monkeypatch, tmp_path):
    """Old/future config files with extra keys must not crash loading."""
    path = use_tmp_config(monkeypatch, tmp_path)
    raw = {
        "converter": {"source_folder": "in/", "bogus_key": 1},
        "streamer": {"protocol": "hls", "nope": True},
        "server": {"port": 8081},
        "future_section": {},
    }
    path.write_text(yaml.dump(raw), encoding="utf-8")
    cfg = load_config()
    assert cfg.converter.source_folder == "in/"
    assert cfg.streamer.protocol == "hls"
    assert cfg.server.port == 8081


def test_auto_start_field_round_trips(monkeypatch, tmp_path):
    use_tmp_config(monkeypatch, tmp_path)
    update_config({"server": {"auto_start": True}})
    assert load_config().server.auto_start is True
    update_config({"server": {"auto_start": False}})
    assert load_config().server.auto_start is False
