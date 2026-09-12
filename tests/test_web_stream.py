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

