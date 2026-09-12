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
    # Web stream without shader upscale (WGC clean client bounds)
    f_web = get_video_filter(is_web=True, shader_upscale=False)
    assert f_web == ["-vf", "format=yuv420p"]

    # Web stream with shader upscale
    f_web_up = get_video_filter(is_web=True, shader_upscale=True)
    assert f_web_up[0] == "-vf"
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


def test_live_relay_infinite_retry_flag():
    relay = LiveRelayStatus(
        id="test_relay_retry",
        name="Test Relay Retry",
        url="http://example.com/live.m3u8",
        port=1981,
        infinite_retry=True,
    )
    assert relay.infinite_retry is True
    d = relay.to_dict()
    assert d["infinite_retry"] is True


def test_get_all_status_includes_infinite_retry(monkeypatch):
    from unittest.mock import MagicMock
    mock_cfg = MagicMock()
    mock_cfg.streamer.live_streams = [
        {"id": "s1", "name": "Stream 1", "url": "http://test/1", "port": 1920, "infinite_retry": True},
        {"id": "s2", "name": "Stream 2", "url": "http://test/2", "port": 1921, "infinite_retry": False},
    ]
    mock_cfg.streamer.global_vpn = {}
    monkeypatch.setattr("app.live_relay.load_config", lambda: mock_cfg)

    statuses = live_relay_manager.get_all_status()
    s1 = next(x for x in statuses if x["id"] == "s1")
    s2 = next(x for x in statuses if x["id"] == "s2")
    assert s1["infinite_retry"] is True
    assert s2["infinite_retry"] is False


def test_socket_reuse_and_clean_wait_closed(monkeypatch):
    import asyncio
    import socket
    from unittest.mock import MagicMock

    async def run_test():
        s = socket.socket()
        s.bind(("", 0))
        free_port = s.getsockname()[1]
        s.close()

        mock_cfg = MagicMock()
        mock_cfg.streamer.live_streams = [
            {"id": "test_sock_reuse", "name": "Sock Reuse", "url": "http://test/live", "port": free_port, "infinite_retry": True}
        ]
        mock_cfg.streamer.global_vpn = {}
        monkeypatch.setattr("app.live_relay.load_config", lambda: mock_cfg)

        async def dummy_loop(relay):
            pass

        monkeypatch.setattr(live_relay_manager, "_auto_restart_loop", dummy_loop)

        res1 = await live_relay_manager.start_stream("test_sock_reuse")
        relay = live_relay_manager.active_relays["test_sock_reuse"]
        assert relay.server is not None
        assert relay.server.is_serving()
        first_server = relay.server

        # Calling start_stream again while relay status changed (e.g. error/reconnecting) must reuse the active server
        relay.status = "error"
        res2 = await live_relay_manager.start_stream("test_sock_reuse")
        assert relay.server is first_server
        assert relay.server.is_serving()
        assert relay.status == "listening"

        # Clean stop awaits wait_closed
        stop_res = await live_relay_manager.stop_stream("test_sock_reuse")
        assert relay.server is None
        assert stop_res["status"] == "stopped"

    asyncio.run(run_test())


def test_auto_restart_loop_infinite_retry_behavior(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock

    async def run_test():
        # Case 1: infinite_retry=False -> Probe failure sets status="error" and exits
        mock_cfg1 = MagicMock()
        mock_cfg1.streamer.live_streams = [
            {"id": "test_no_retry", "name": "No Retry", "url": "http://test/stream", "port": 1990, "infinite_retry": False}
        ]
        mock_cfg1.streamer.global_vpn = {}
        monkeypatch.setattr("app.live_relay.load_config", lambda: mock_cfg1)
        monkeypatch.setattr("app.ffmpeg_setup.probe_source_codec", lambda *args, **kwargs: "404 Not Found")

        relay1 = LiveRelayStatus(id="test_no_retry", name="No Retry", url="http://test/stream", port=1990, status="listening")
        await live_relay_manager._auto_restart_loop(relay1)
        assert relay1.status == "error"
        assert "404" in relay1.error

        # Case 2: infinite_retry=True -> Probe failure sets status="reconnecting", retries, and succeeds when source becomes available
        mock_cfg2 = MagicMock()
        mock_cfg2.streamer.live_streams = [
            {"id": "test_with_retry", "name": "With Retry", "url": "http://test/stream", "port": 1991, "infinite_retry": True}
        ]
        mock_cfg2.streamer.global_vpn = {}
        monkeypatch.setattr("app.live_relay.load_config", lambda: mock_cfg2)

        probe_calls = 0
        def dynamic_probe(*args, **kwargs):
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls == 1:
                return "Connection refused"
            return "h264"

        monkeypatch.setattr("app.ffmpeg_setup.probe_source_codec", dynamic_probe)

        relay2 = LiveRelayStatus(id="test_with_retry", name="With Retry", url="http://test/stream", port=1991, status="listening", infinite_retry=True)

        async def fast_sleep(sec):
            if probe_calls >= 2:
                relay2.status = "stopped"  # stop loop after probe recovery

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        await live_relay_manager._auto_restart_loop(relay2)
        assert probe_calls >= 2

    asyncio.run(run_test())
