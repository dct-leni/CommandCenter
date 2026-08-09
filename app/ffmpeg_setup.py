"""
FFmpeg and MediaMTX path helpers.
Binaries are expected in bin/ folder — run setup_binaries.bat once to download them.
"""

import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

BIN_DIR = Path(__file__).parent.parent / "bin"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"
FFPROBE_EXE = BIN_DIR / "ffprobe.exe"
MEDIAMTX_EXE = BIN_DIR / "mediamtx.exe"
_FFMPEG_INSTALLED = None
_MEDIAMTX_INSTALLED = None
_NVENC_AVAILABLE = None



def is_ffmpeg_installed() -> bool:
    """Check if portable FFmpeg is available in bin/."""
    global _FFMPEG_INSTALLED
    if _FFMPEG_INSTALLED is None:
        _FFMPEG_INSTALLED = FFMPEG_EXE.exists() and FFPROBE_EXE.exists()
    return _FFMPEG_INSTALLED


def is_mediamtx_installed() -> bool:
    """Check if portable MediaMTX is available in bin/."""
    global _MEDIAMTX_INSTALLED
    if _MEDIAMTX_INSTALLED is None:
        _MEDIAMTX_INSTALLED = MEDIAMTX_EXE.exists()
    return _MEDIAMTX_INSTALLED



def is_nvenc_available() -> bool:
    """Check if hardware NVENC encoding (`h264_nvenc`) is supported on this machine."""
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE

    if not is_ffmpeg_installed():
        _NVENC_AVAILABLE = False
        return _NVENC_AVAILABLE

    try:
        res = subprocess.run(
            [
                str(FFMPEG_EXE),
                "-v", "error",
                "-f", "lavfi",
                "-i", "nullsrc=s=640x360:d=0.05",
                "-c:v", "h264_nvenc",
                "-f", "null",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        _NVENC_AVAILABLE = (res.returncode == 0)
    except Exception:
        _NVENC_AVAILABLE = False

    return _NVENC_AVAILABLE


_QSV_AVAILABLE = None
_BEST_ENCODER = None


def is_qsv_available() -> bool:
    """Check if Intel QSV hardware encoding (`h264_qsv`) is supported on this machine."""
    global _QSV_AVAILABLE
    if _QSV_AVAILABLE is not None:
        return _QSV_AVAILABLE

    if not is_ffmpeg_installed():
        _QSV_AVAILABLE = False
        return _QSV_AVAILABLE

    try:
        res = subprocess.run(
            [
                str(FFMPEG_EXE),
                "-v", "error",
                "-f", "lavfi",
                "-i", "nullsrc=s=640x360:d=0.05",
                "-c:v", "h264_qsv",
                "-f", "null",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        _QSV_AVAILABLE = (res.returncode == 0)
    except Exception:
        _QSV_AVAILABLE = False

    return _QSV_AVAILABLE


def get_best_encoder() -> str:
    """Return the best supported encoder: 'h264_nvenc', 'h264_qsv', or 'libx264'."""
    global _BEST_ENCODER
    if _BEST_ENCODER is not None:
        return _BEST_ENCODER

    if is_nvenc_available():
        _BEST_ENCODER = "h264_nvenc"
    elif is_qsv_available():
        _BEST_ENCODER = "h264_qsv"
    else:
        _BEST_ENCODER = "libx264"

    logger.info(f"Auto-detected hardware acceleration: { _BEST_ENCODER }")
    return _BEST_ENCODER


def get_ffmpeg_path() -> str:
    """Return the path to the FFmpeg executable."""
    return str(FFMPEG_EXE)


def get_ffprobe_path() -> str:
    """Return the path to the FFprobe executable."""
    return str(FFPROBE_EXE)


def get_mediamtx_path() -> str:
    """Return the path to the MediaMTX executable."""
    return str(MEDIAMTX_EXE)


def get_binaries_status() -> dict:
    """Return availability status of all binaries."""
    return {
        "ffmpeg": is_ffmpeg_installed(),
        "mediamtx": is_mediamtx_installed(),
        "ffmpeg_path": str(FFMPEG_EXE),
        "mediamtx_path": str(MEDIAMTX_EXE),
        "nvenc_available": is_nvenc_available(),
        "qsv_available": is_qsv_available(),
        "best_encoder": get_best_encoder(),
    }


def get_encoding_params(
    encoder: str,
    source_bitrate: Optional[int] = None,
    mode: str = "converter"
) -> list:
    """
    Unified encoding parameter generator for video conversion, live relays, and web streams.

    Modes:
      - 'converter': File transcode (NVENC preset p5, VBR, cq 24, spatial/temporal AQ)
      - 'relay':     Live stream re-encode (NVENC preset p5, VBR 2.8M, temporal AQ)
      - 'web':       GDIGrab screen capture (NVENC preset p5, single-pass CBR 2.8M, NO AQ buffers)
    """
    target_b_bps = 2_800_000   # 2.8 Mbps default
    max_b_bps    = 3_200_000   # 3.2 Mbps default
    buf_b_bps    = 6_400_000   # 6.4 Mbps default

    if source_bitrate and 0 < source_bitrate < target_b_bps:
        # Match source bitrate 1:1 to preserve original file size without padding
        target_b_bps = max(300_000, source_bitrate)
        max_b_bps    = int(target_b_bps * 1.1)
        buf_b_bps    = max_b_bps * 2

    def _format_rate(rate_bps: int) -> str:
        if rate_bps % 1_000_000 == 0:
            return f"{rate_bps // 1_000_000}M"
        return f"{int(rate_bps / 1000)}k"

    target_b_str = _format_rate(target_b_bps)
    max_b_str    = _format_rate(max_b_bps)
    buf_b_str    = _format_rate(buf_b_bps)

    if encoder == "h264_nvenc":
        preset = "p4" if mode == "web" else "p5"
        params = [
            "-c:v", "h264_nvenc",
            "-preset", preset,
            "-profile:v", "high",
            "-b:v", target_b_str,
            "-maxrate", max_b_str if mode != "web" else target_b_str,
            "-bufsize", buf_b_str,
            "-g", "60",
        ]
        if mode == "web":
            # GDIGrab requires single-pass CBR without AQ/lookahead buffers to prevent frame stalls.
            # Using -bf 0 (No B-Frames) cuts GPU Video Engine load by ~40% and reduces encoding latency.
            params.extend(["-rc", "cbr", "-bf", "0"])
        elif mode == "converter":
            params.extend(["-rc", "vbr", "-cq", "24", "-spatial-aq", "1", "-temporal-aq", "1"])
        elif mode == "relay":
            params.extend(["-rc", "vbr", "-temporal-aq", "1"])
        return params

    elif encoder == "h264_qsv":
        params = [
            "-c:v", "h264_qsv",
            "-preset", "fast" if mode == "web" else "medium",
            "-profile:v", "high",
            "-b:v", target_b_str,
            "-maxrate", max_b_str,
            "-bufsize", buf_b_str,
            "-g", "60",
        ]
        if mode == "converter":
            params.extend(["-look_ahead", "1", "-look_ahead_depth", "15"])
        return params

    elif encoder == "libx264":
        preset = "ultrafast" if mode == "web" else ("fast" if mode == "relay" else "medium")
        params = [
            "-c:v", "libx264",
            "-preset", preset,
            "-profile:v", "high",
            "-b:v", target_b_str,
            "-maxrate", max_b_str,
            "-bufsize", buf_b_str,
            "-g", "60",
        ]
        if mode == "web":
            params.extend(["-tune", "zerolatency"])
        elif mode == "converter":
            params.extend(["-crf", "21"])
        elif mode == "relay":
            params.extend(["-crf", "23"])
        return params

    else:
        return ["-c:v", "copy"]


def get_relay_params() -> list:
    """Return stream copy parameters for the live relay (zero GPU usage)."""
    return ["-c:v", "copy"]


def get_relay_encoding_params(encoder: str) -> list:
    """Alias for get_encoding_params with mode='relay'."""
    return get_encoding_params(encoder, mode="relay")


def get_screen_capture_params(encoder: str) -> list:
    """Alias for get_encoding_params with mode='web'."""
    return get_encoding_params(encoder, mode="web")


def get_audio_params(is_web: bool = False) -> list:
    """Return standardized audio encoding/filter parameters for live stream copy vs web streams."""
    if is_web:
        return [
            "-c:a", "aac",
            "-b:a", "192k",
            "-af", "adelay=350|350,aresample=async=1000:min_hard_comp=0.100000:first_pts=0"
        ]
    return ["-c:a", "copy"]


def format_ffmpeg_headers(url: str) -> str:
    """Auto-generate standard User-Agent, Referer, and Origin headers for FFmpeg/ffprobe HTTP requests."""
    lines = [
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ]
    if url.lower().startswith("http://") or url.lower().startswith("https://"):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"
            lines.append(f"Referer: {base_url}")
            lines.append(f"Origin: {parsed.scheme}://{parsed.netloc}")
        except Exception:
            pass

    return "\r\n".join(lines) + "\r\n"


def probe_source_codec(url: str, timeout: int = 8, proxy_url: Optional[str] = None) -> str:
    """
    Probe the video codec of a stream URL using ffprobe.
    Returns a lowercase codec name, e.g. 'h264', 'hevc', 'mpeg2video', or 'unknown'.
    Result is used to decide whether stream-copy or re-encode is needed.
    """
    try:
        cmd = [
            str(FFPROBE_EXE),
            "-v", "error",
        ]

        if proxy_url:
            if proxy_url.startswith("socks5://") or proxy_url.startswith("socks4://"):
                cmd.extend(["-socks_proxy", proxy_url])
            else:
                cmd.extend(["-http_proxy", proxy_url])

        formatted_headers = format_ffmpeg_headers(url)
        if formatted_headers and (url.lower().startswith("http://") or url.lower().startswith("https://")):
            cmd.extend(["-headers", formatted_headers])

        is_network_input = any(url.lower().startswith(proto) for proto in ("http://", "https://", "rtsp://", "rtmp://", "udp://"))
        is_hls = ".m3u8" in url.lower()

        if is_network_input:
            cmd.extend([
                "-probesize", "5M",
                "-analyzeduration", "5M",
            ])

        if (url.lower().startswith("http://") or url.lower().startswith("https://")) and not is_hls:
            cmd.extend([
                "-timeout", f"{int(timeout * 1000000)}",
            ])

        cmd.extend([
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
        ])

        if is_hls:
            cmd.extend([
                "-allowed_extensions", "ALL",
                "-allowed_segment_extensions", "ALL",
                "-extension_picky", "0",
            ])

        cmd.append(url)

        env = os.environ.copy()
        if proxy_url:
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url

        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout + 2,
            env=env,
        )

        if res.returncode != 0:
            err_msg = res.stderr.decode("utf-8", errors="replace").strip()
            logger.warning(f"ffprobe returned code {res.returncode} for {url}: {err_msg}")

        codec = res.stdout.decode("utf-8", errors="replace").strip()
        codec = codec.splitlines()[0].strip().lower() if codec else "unknown"
        return codec if codec else "unknown"
    except Exception as e:
        logger.warning(f"ffprobe execution error for {url}: {e}")
        return "unknown"


def parse_ffmpeg_progress(line: str) -> Optional[float]:
    """Parse time=HH:MM:SS.ms string from FFmpeg progress output and return total seconds."""
    match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
    if match:
        try:
            hours, minutes, seconds = map(float, match.groups())
            return hours * 3600 + minutes * 60 + seconds
        except (ValueError, TypeError):
            pass
    return None
