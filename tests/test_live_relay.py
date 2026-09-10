import pytest
from app.live_relay import LiveRelayStatus, live_relay_manager
from app.ffmpeg_setup import (
    get_video_filter,
    is_wgc_available,
    get_videocapture_path,
    format_ffmpeg_headers,
)


def test_live_relay_status_defaults():
    relay = LiveRelayStatus(
        id="test_relay_1",
        name="Test Relay",
        url="http://example.com/live.m3u8",
        port=1980,
    )
    assert relay.status == "stopped"
    assert relay.error is None
    assert relay.fps == 0.0
    assert relay.bitrate == "0kbits/s"
    assert relay.capture_process is None

    d = relay.to_dict()
    assert d["id"] == "test_relay_1"
    assert d["name"] == "Test Relay"
    assert d["port"] == 1980
    assert d["status"] == "stopped"


def test_video_filter_generation():
    # Web stream without shader upscale
    f_web = get_video_filter(is_web=True, shader_upscale=False)
    assert f_web == ["-vf", "crop=iw:ih-38:0:38,format=yuv420p"]

    # Web stream with shader upscale
    f_web_up = get_video_filter(is_web=True, shader_upscale=True)
    assert f_web_up[0] == "-vf"
    assert "crop=iw:ih-38:0:38" in f_web_up[1]
    assert "scale=1920:1080:flags=lanczos" in f_web_up[1]
    assert "unsharp=3:3:0.5:3:3:0.0" in f_web_up[1]
    assert "format=yuv420p" in f_web_up[1]

    # Non-web stream without shader upscale
    f_norm = get_video_filter(is_web=False, shader_upscale=False)
    assert f_norm == ["-vf", "format=yuv420p"]

    # Non-web stream with shader upscale
    f_norm_up = get_video_filter(is_web=False, shader_upscale=True)
    assert "scale=1920:1080:flags=lanczos" in f_norm_up[1]


def test_format_ffmpeg_headers():
    headers = format_ffmpeg_headers("https://example.com/live/stream.m3u8")
    assert "User-Agent:" in headers
    assert "Referer: https://example.com/" in headers
    assert "Origin: https://example.com" in headers


def test_wgc_binary_detection():
    # WGC binary was built to bin/app_videocapture.exe
    vcap_path = get_videocapture_path()
    assert "app_videocapture.exe" in vcap_path
    assert is_wgc_available() is True
