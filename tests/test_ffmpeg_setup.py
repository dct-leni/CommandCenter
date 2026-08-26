import pytest
from app.ffmpeg_setup import parse_ffmpeg_progress, get_encoding_params, format_ffmpeg_headers

def test_parse_ffmpeg_progress():
    # Standard time format
    line1 = "frame=  120 fps= 30 q=28.0 size=    1024kB time=00:01:23.45 bitrate= 100.5kbits/s speed=1.5x"
    sec1 = parse_ffmpeg_progress(line1)
    assert sec1 is not None
    assert round(sec1, 2) == 83.45

    # Another standard time line
    line2 = "frame=  500 fps= 60 q=20.0 size=    5120kB time=01:00:05.10 bitrate= 2500kbits/s speed=2.0x"
    sec2 = parse_ffmpeg_progress(line2)
    assert sec2 is not None
    assert round(sec2, 2) == 3605.10

    # No time in line
    line3 = "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'input.mp4':"
    assert parse_ffmpeg_progress(line3) is None

def test_get_encoding_params_cpu():
    params = get_encoding_params("libx264", source_bitrate=3000000)
    assert "-c:v" in params
    idx = params.index("-c:v")
    assert params[idx + 1] == "libx264"
    assert "-preset" in params
    assert "-crf" in params

def test_get_encoding_params_nvenc():
    params = get_encoding_params("h264_nvenc", source_bitrate=5000000)
    assert "-c:v" in params
    idx = params.index("-c:v")
    assert params[idx + 1] == "h264_nvenc"
    assert "-spatial-aq" in params or "-spatial_aq" in params or "-preset" in params

def test_format_ffmpeg_headers():
    url = "http://example.com/live/playlist.m3u8"
    headers_str = format_ffmpeg_headers(url)
    assert "User-Agent:" in headers_str
    assert "\r\n" in headers_str
