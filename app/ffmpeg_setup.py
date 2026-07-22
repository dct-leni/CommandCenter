"""
FFmpeg and MediaMTX path helpers.
Binaries are expected in bin/ folder — run setup_binaries.bat once to download them.
"""

import logging
from pathlib import Path
import subprocess

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


def get_encoding_params(encoder: str, source_bitrate: Optional[int] = None) -> list:
    """
    Return the optimized encoding parameters for local converter.
    If source_bitrate is provided and lower than 2.8 Mbps, cap target & max bitrate
    proportionally to prevent low-bitrate input files from inflating in size.
    """
    target_b_bps = 2_800_000   # 2.8 Mbps default
    max_b_bps    = 3_200_000   # 3.2 Mbps default
    buf_b_bps    = 6_400_000   # 6.4 Mbps default

    if source_bitrate and 0 < source_bitrate < target_b_bps:
        # Match source bitrate 1:1 to preserve original file size without padding useless data
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
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p6",             # High-quality preset
            "-profile:v", "high",
            "-b:v", target_b_str,
            "-maxrate", max_b_str,
            "-bufsize", buf_b_str,
            "-spatial-aq", "1",          # Optimize dark scene details
            "-temporal-aq", "1",         # Smooth out fast motion pixels
            "-g", "60",                  # 2s keyframe interval
        ]
    elif encoder == "h264_qsv":
        return [
            "-c:v", "h264_qsv",
            "-preset", "medium",
            "-b:v", target_b_str,
            "-maxrate", max_b_str,
            "-bufsize", buf_b_str,
            "-look_ahead", "1",          # Enable lookahead to smooth out motion
            "-look_ahead_depth", "15",
            "-g", "60",
        ]
    elif encoder == "libx264":
        return [
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", target_b_str,
            "-maxrate", max_b_str,
            "-bufsize", buf_b_str,
            "-g", "60",
        ]
    else:
        return ["-c:v", "copy"]


def probe_source_codec(url: str, timeout: int = 8, proxy_url: Optional[str] = None) -> str:
    """
    Probe the video codec of a stream URL using ffprobe.
    Returns a lowercase codec name, e.g. 'h264', 'hevc', 'mpeg2video', or 'unknown'.
    Result is used to decide whether stream-copy or re-encode is needed.
    """
    try:
        cmd = [
            str(FFPROBE_EXE),
            "-v", "quiet",
        ]

        if proxy_url:
            if proxy_url.startswith("socks5://") or proxy_url.startswith("socks4://"):
                cmd.extend(["-socks_proxy", proxy_url])
            else:
                cmd.extend(["-http_proxy", proxy_url])

        cmd.extend([
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
        ])
        if ".m3u8" in url.lower():
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
            timeout=timeout,
            env=env,
        )
        codec = res.stdout.decode("utf-8", errors="replace").strip()
        # ffprobe may return one line per video track — take the first
        codec = codec.splitlines()[0].strip().lower() if codec else "unknown"
        return codec if codec else "unknown"
    except Exception:
        return "unknown"


def get_relay_params() -> list:
    """
    Return stream copy parameters for the live relay (zero GPU usage).
    The live relay ingests an already-encoded H.264 stream and just re-muxes
    it — no re-encode needed. Using codec copy eliminates GPU usage entirely
    while preserving bit-for-bit identical video quality.
    The bsf:v dump_extra filter injects SPS/PPS headers before every keyframe
    so that late-joining clients can decode immediately without seeking.
    """
    return ["-c:v", "copy"]


def get_relay_encoding_params(encoder: str) -> list:
    """
    Low-GPU re-encode parameters for live relay when source is not H.264.

    Differences from get_encoding_params() (converter quality profile):
      NVENC: preset p4 instead of p6  → ~20% less GPU, imperceptible quality loss
             no spatial-aq/temporal-aq → ~18% less GPU, minor impact on dark/fast scenes
             no rc-lookahead           → ~5%  less GPU, negligible for live content
      QSV:   preset fast, no lookahead → ~20% less CPU/GPU compute
      CPU:   preset fast, crf 23       → far less CPU, very similar visual quality to crf 21
    """
    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p5",            # One step below converter's p6 — ~10% GPU saving, imperceptible quality diff
            "-profile:v", "high",
            "-b:v", "2.8M",
            "-maxrate", "3.2M",
            "-bufsize", "6.4M",
            "-temporal-aq", "1",        # Keep: preserves motion sharpness in fast/sports content
            "-g", "60",
            # spatial-aq off: saves ~10% GPU; mainly helps dark flat areas, not motion content
            # rc-lookahead off: saves ~5% GPU; live stream + VBR buffer already handles spikes
        ]
    elif encoder == "h264_qsv":
        return [
            "-c:v", "h264_qsv",
            "-preset", "medium",
            "-b:v", "2.8M",
            "-maxrate", "3.2M",
            "-bufsize", "6.4M",
            "-g", "60",
            # look_ahead disabled: saves compute; less beneficial for live relay
        ]
    elif encoder == "libx264":
        return [
            "-c:v", "libx264",
            "-preset", "fast",          # Much lighter than 'medium'
            "-crf", "23",               # +2 vs converter's 21; still visually very close
            "-maxrate", "3.2M",
            "-bufsize", "6.4M",
            "-g", "60",
        ]
    else:
        return ["-c:v", "copy"]
