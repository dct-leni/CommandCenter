import zipfile
import json
from pathlib import Path
from app.web_stream import _create_firefox_profile, web_stream_manager


def test_create_firefox_profile_from_assets(tmp_path):
    profile_dir = tmp_path / "test_profile"
    _create_firefox_profile(profile_dir, proxy_url=None, stream_id="test_stream_42")

    # Verify user.js was created and contains essential prefs
    user_js = profile_dir / "user.js"
    assert user_js.exists()
    content = user_js.read_text(encoding="utf-8")
    assert 'media.autoplay.default' in content
    assert 'dom.suspend_inactive.enabled' in content
    assert 'dom.ipc.processCount", 1' in content
    assert 'network.proxy.type", 0' in content

    # Verify chrome directory and CSS files
    chrome_dir = profile_dir / "chrome"
    assert (chrome_dir / "userChrome.css").exists()
    assert (chrome_dir / "userContent.css").exists()
    user_content = (chrome_dir / "userContent.css").read_text(encoding="utf-8")
    assert ".cc-full-window-player" in user_content

    # Verify extension xpi
    ext_file = profile_dir / "extensions" / "visibility@commandcenter.local.xpi"
    assert ext_file.exists()
    with zipfile.ZipFile(ext_file, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert "content.js" in namelist
        manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest_data["manifest_version"] == 2
        content_js = zf.read("content.js").decode("utf-8")
        assert "visibilitychange" in content_js


def test_create_firefox_profile_with_socks5_proxy(tmp_path):
    profile_dir = tmp_path / "proxy_profile"
    _create_firefox_profile(profile_dir, proxy_url="socks5h://127.0.0.1:1080", stream_id="stream_proxy")

    user_js = profile_dir / "user.js"
    content = user_js.read_text(encoding="utf-8")
    assert 'network.proxy.type", 1' in content
    assert 'network.proxy.socks", "127.0.0.1"' in content
    assert 'network.proxy.socks_port", 1080' in content
    assert 'network.proxy.socks_remote_dns", true' in content


def test_create_firefox_profile_with_http_proxy(tmp_path):
    profile_dir = tmp_path / "http_proxy_profile"
    _create_firefox_profile(profile_dir, proxy_url="http://127.0.0.1:10501", stream_id="stream_http_proxy")

    user_js = profile_dir / "user.js"
    content = user_js.read_text(encoding="utf-8")
    assert 'network.proxy.type", 1' in content
    assert 'network.proxy.http", "127.0.0.1"' in content
    assert 'network.proxy.http_port", 10501' in content
    assert 'network.proxy.ssl", "127.0.0.1"' in content
    assert 'network.proxy.ssl_port", 10501' in content
    assert 'network.proxy.share_proxy_settings", true' in content

    # Verify no syntax error remnants from python string literals
    assert "';" not in content
    assert "'," not in content


def test_screen_capture_params_resolution_scaling():
    from app.ffmpeg_setup import get_screen_capture_params, get_relay_encoding_params

    # 720p profile checks
    params_720 = get_screen_capture_params("libx264", resolution="720p")
    assert "-b:v" in params_720
    idx_b = params_720.index("-b:v")
    assert params_720[idx_b + 1] == "2800k"
    idx_buf = params_720.index("-bufsize")
    assert params_720[idx_buf + 1] == "6400k"

    # 1080p profile checks
    params_1080 = get_screen_capture_params("libx264", resolution="1080p")
    assert "-b:v" in params_1080
    idx_b1080 = params_1080.index("-b:v")
    assert params_1080[idx_b1080 + 1] == "5M"
    idx_buf1080 = params_1080.index("-bufsize")
    assert params_1080[idx_buf1080 + 1] == "10M"

    # Relay transcode 1080p profile checks
    relay_1080 = get_relay_encoding_params("libx264", resolution="1080p")
    idx_buf_relay = relay_1080.index("-bufsize")
    assert relay_1080[idx_buf_relay + 1] == "9M"


def test_create_and_update_stream_resolution():
    from app.routers.live import LiveStreamCreateRequest, LiveStreamUpdateRequest

    req = LiveStreamCreateRequest(
        name="Test 1080p Web Stream",
        url="https://test.com",
        port=1929,
        stream_type="web",
        resolution="1080p",
    )
    assert req.resolution == "1080p"

    update_req = LiveStreamUpdateRequest(resolution="720p")
    assert update_req.resolution == "720p"


def test_close_browser_protects_other_stream_pids_and_hwnds():
    """Verify close_browser never terminates an HWND or PID owned by another stream."""
    stream_a = "live_stream_a"
    stream_b = "live_stream_b"

    # Register two fake active streams
    web_stream_manager.window_hwnds[stream_a] = 111111
    web_stream_manager.window_hwnds[stream_b] = 222222

    # Closing stream A should remove stream A's hwnd and leave stream B's hwnd untouched
    web_stream_manager.close_browser(stream_a)
    assert stream_a not in web_stream_manager.window_hwnds
    assert web_stream_manager.window_hwnds.get(stream_b) == 222222

    # Clean up
    web_stream_manager.close_browser(stream_b)


def test_get_window_hwnd_clears_dead_hwnd(monkeypatch):
    """Verify get_window_hwnd removes dead HWND when user32.IsWindow returns False."""
    stream_id = "test_dead_hwnd_stream"
    web_stream_manager.window_hwnds[stream_id] = 999999

    import app.web_stream as ws
    if hasattr(ws, "user32"):
        monkeypatch.setattr(ws.user32, "IsWindow", lambda hwnd: 0)

    # Calling get_window_hwnd should detect dead window, clear it from dict, and not return 999999
    # (Since wait_for_window_title won't find a real window for this dummy stream, it returns None)
    result = web_stream_manager.get_window_hwnd(stream_id, "Test", "http://fake.test", "720p")
    assert result != 999999
    assert stream_id not in web_stream_manager.window_hwnds


def test_ensure_firefox_policies(tmp_path):
    """Verify policies.json is generated with required enterprise policies."""
    from app.web_stream import _ensure_firefox_policies

    fake_portapps_root = tmp_path / "phyrox"
    fake_portapps_app = fake_portapps_root / "app"
    fake_portapps_data = fake_portapps_root / "data"
    fake_portapps_app.mkdir(parents=True)
    fake_portapps_data.mkdir(parents=True)
    fake_exe = fake_portapps_app / "firefox.exe"
    fake_exe.touch()

    _ensure_firefox_policies(fake_exe)

    dist_policy = fake_portapps_app / "distribution" / "policies.json"
    data_policy = fake_portapps_data / "policies.json"
    assert dist_policy.exists()
    assert data_policy.exists()

    data = json.loads(dist_policy.read_text(encoding="utf-8"))
    assert data["policies"]["DisableAppUpdate"] is True
    assert data["policies"]["DontCheckDefaultBrowser"] is True
    assert data["policies"]["DisableTelemetry"] is True


def test_ensure_phyrox_config(tmp_path, monkeypatch):
    """Verify ensure_phyrox_config auto-generates phyrox-portable.yml and policies."""
    fake_bin = tmp_path / "bin"
    fake_firefox_dir = fake_bin / "firefox"
    fake_firefox_dir.mkdir(parents=True)
    fake_phyrox = fake_firefox_dir / "phyrox-portable.exe"
    fake_phyrox.touch()

    # Create dummy sample yml
    sample_yml = fake_firefox_dir / "phyrox-portable.sample.yml"
    sample_yml.write_text("common:\n  args: []\napp:\n  multiple_instances: false\n", encoding="utf-8")

    import app.web_stream as ws
    monkeypatch.setattr(ws, "_BASE_DIR", tmp_path)

    web_stream_manager.ensure_phyrox_config()

    target_yml = fake_firefox_dir / "phyrox-portable.yml"
    assert target_yml.exists()
    content = target_yml.read_text(encoding="utf-8")
    assert "multiple_instances: true" in content
    assert "cleanup: false" in content
    assert "disable_firefox_studies" not in content

