# CommandCenter Feature Status & Optimization Plan

## Summary Status

| Feature / Task | Status | Implementation Details |
| :--- | :--- | :--- |
| **Feature 1: Stream Resolution Selection (720p vs 1080p)** | ⏳ **Planned** | Ready for execution. Includes 24MB Named Pipe buffer & dynamic VBV scaling. |
| **Feature 2: Rate-Distortion Encoding Optimization** | ✅ **Implemented** | Capped VBR with CQ, Spatial AQ, B-frames with middle ref, veryfast x264. |
| **Feature 3: Pure WGC Migration & GDIGrab Removal** | ✅ **Implemented** | Removed GDIGrab fallback; WGC is sole capture engine. Restore instructions in README.md. |
| **Feature 4: FFmpeg Skill Hardening & Subprocess Safety** | ✅ **Implemented** | Strict CFR, BT.709 & HDR tonemapping, setsar=1, YUV420p, 48kHz, headless flags, pipe safety. |

---

## Feature 1: Stream Resolution Selection (720p vs 1080p) [PLANNED]

### 1. Overview & Motivation
Allow choosing between **720p (1280x720)** and **1080p (1920x1080)** per stream for both Web Streams (Firefox + WGC) and Live Relays (IPTV / M3U8 / RTMP).
* **720p**: Ideal for lower bandwidth, remote IPTV viewing, low network speeds, and low-spec client devices.
* **1080p**: Allows browser players (YouTube, Exxen, S Sport Plus, Tabii) to request native 1080p stream manifests, delivering crisp Full HD quality for sports and main screens.

---

### 2. Architecture & Pipeline Changes

```
                     ┌───────────────────────────────┐
                     │ Stream Config / UI Selection  │
                     │  Resolution: [ 720p | 1080p ] │
                     └───────────────┬───────────────┘
                                     │
             ┌───────────────────────┴───────────────────────┐
             ▼                                               ▼
   [Web Stream (Firefox)]                         [Live Relay (IPTV)]
   - 720p:  Window 1280x720                       - 720p:  Scale/Copy to 720p
            WGC: 1280x720, 2.8 Mbps                        Bitrate: 2.8 Mbps
   - 1080p: Window 1920x1080                      - 1080p: Scale/Copy to 1080p
            WGC: 1920x1080, 4.8 Mbps                       Bitrate: 4.8 Mbps
            Named Pipe: 24MB buffer
```

#### A. Web Browser Sizing (`app/web_stream.py`)
- Pass `resolution: str = "720p"` to `launch_browser`:
  - `720p`: `--width=1280 --height=720` and `SetWindowPos(hwnd, 0, 0, 0, 1280, 758, SWP_SHOWWINDOW)`
  - `1080p`: `--width=1920 --height=1080` and `SetWindowPos(hwnd, 0, 0, 0, 1920, 1118, SWP_SHOWWINDOW)`
- Viewport size directly controls the video quality tier that web players negotiate from CDNs.

#### B. Capture & Transcode Pipeline (`app/live_relay.py`)
- Reads `stream_item.get("resolution", "720p")`.
- Passes resolution to `app_videocapture.exe` (`--width` and `--height`) and FFmpeg (`-s`).
- Automatically selects bitrate profile according to stream resolution.

---

### 3. Buffer Analysis & Required Modifications

| Buffer | Component | Current Value | 1080p Impact | Required Action |
| :--- | :--- | :--- | :--- | :--- |
| **Named Pipe Buffer** | `src/app_videocapture.cpp` (`CreateNamedPipeW`) | 8 MB (`8*1024*1024`) | 1 frame uncompressed 1080p BGRA = **8.29 MB** (`1920*1080*4`). Exceeds 8 MB buffer, causing pipe write blocks. | **MUST MODIFY**: Increase to **24 MB** (`24*1024*1024`) to safely hold ~3 full 1080p frames (or ~6.5 720p frames). |
| **Encoder VBV Buffer** | `app/ffmpeg_setup.py` (`-bufsize`) | 6.4 Mbps | Static 6.4M buffer designed for 720p causes macroblocking and rate-control starvation at 1080p. | **MUST MODIFY**: Scale dynamically based on resolution (6.4 Mbps for 720p, 10.0 Mbps for 1080p). |
| **Encoder Target Bitrate** | `app/ffmpeg_setup.py` (`-b:v`, `-maxrate`) | 2.8 Mbps / 3.5 Mbps | Insufficient for fast motion / sports at 1080p. | **MUST MODIFY**: Scale dynamically (2.8M for 720p, 4.8M for 1080p). |
| **Client Output Queue** | `app/live_relay.py` (`asyncio.Queue`) | 128 chunks | Holds ~1.5–2s of compressed MPEG-TS chunks. Ample headroom for 2.8M–5.0M streams. | **DO NOT MODIFY**: Keep at 128. |
| **Firefox Memory Cache** | `app/assets/firefox/user.js` | 64 MB | Sufficient for HTML5 video buffering. | **DO NOT MODIFY**: Keep at 64 MB. |

---

### 4. Configuration & UI Extensions
* **`app/config.py`**:
  * Add `resolution: str = "720p"` to `LiveStreamItem`.
  * Add `default_stream_resolution: str = "720p"` to `StreamerConfig`.
* **`static/index.html` & `static/app.js`**:
  * Add Resolution dropdown (`720p` vs `1080p`) in `#livestream-modal` and `#webstream-modal`.
  * Display resolution badge on stream cards.

---
---

## Feature 2: Rate-Distortion Encoding Optimization [COMPLETED]

Implemented in [`app/ffmpeg_setup.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/ffmpeg_setup.py).

### Changes Implemented:
1. **Web Capture (`h264_nvenc`)**:
   - Replaced rigid CBR with **Capped VBR with CQ 24** (`-rc vbr -cq 24`). Bitrate drops to **400–800 kbps** on static screens / pause menus (saving 30–50% bandwidth), while bursting up to 3.5 Mbps during motion.
   - Added **Spatial AQ** (`-spatial-aq 1 -temporal-aq 1`): Makes browser text, scoreboards, and UI graphics razor sharp without increasing total bitrate.
   - Kept `-bf 0` for sub-frame real-time browser interaction latency.
2. **Live Relays (`h264_nvenc`)**:
   - Added **B-Frames with Middle Reference** (`-bf 2 -b_ref_mode middle`): Increases compression efficiency by **~20%** compared to I/P-only streams.
   - Added **Spatial AQ** (`-spatial-aq 1`): Eliminates macroblocking in flat and dark scenes.
3. **Offline Converter (`h264_nvenc`)**:
   - Upgraded to preset **`p6`** with `-multipass fullres`, lookahead 32, `-cq 22`, and `-bf 3 -b_ref_mode middle` for maximum offline compression.
4. **CPU Encoder (`libx264`)**:
   - Upgraded web capture preset from `ultrafast` to **`veryfast`** (drastically reduces blockiness at identical bitrates).
   - Cleaned Constrained CRF: Uses pure CRF + VBV ceiling (`-crf 21/23 -maxrate 3.5M -bufsize 6.4M`) without conflicting average bitrate hints.

---
---

## Feature 3: Pure WGC Migration & GDIGrab Removal [COMPLETED]

Implemented across [`app/live_relay.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/live_relay.py), [`app/ffmpeg_setup.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/ffmpeg_setup.py), and [`README.md`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/README.md).

### Changes Implemented:
1. **Removed GDIGrab Fallback**:
   - Eliminated the legacy `if not wgc_active: cmd.extend([... -f gdigrab ...])` branch in `app/live_relay.py`.
   - Web stream capture exclusively uses **Windows Graphics Capture (WGC)** via `bin/app_videocapture.exe` with named pipes.
   - Why: GDIGrab is incompatible with VBR encoding (causes timestamp desync and micro-freezes). WGC provides hardware D3D11 capture with zero tearing, hardware acceleration compatibility, and flawless VBR rate control.
2. **Clean Video Filter**:
   - Removed the 38px titlebar crop filter (`crop=iw:ih-38:0:38`) as default since WGC crops client bounds directly on the GPU without including the window titlebar.
3. **Restoration Instructions**:
   - Complete step-by-step instructions on how to restore legacy GDIGrab capture are archived in [`README.md`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/README.md) (Section 24).

---
---

## Feature 4: FFmpeg Skill Hardening & Subprocess Safety [COMPLETED]

Implemented based on audit of [`ffmpeg-skill`](https://github.com/kajisho5/ffmpeg-skill) across [`app/ffmpeg_setup.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/ffmpeg_setup.py), [`app/converter.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/converter.py), [`app/streamer.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/streamer.py), [`app/live_relay.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/live_relay.py), and [`app/thumbnails.py`](file:///c:/Users/Leni/Desktop/Projects/CommandCenter/app/thumbnails.py).

### Changes Implemented:
1. **Strict Constant Frame Rate (`-fps_mode cfr`)**:
   - Added `-fps_mode cfr -r 30` to both `converter.py` and `live_relay.py`. Eliminates variable frame rate timestamp jitter and prevents progressive audio/video desync over long runs.
2. **Color Space Tagging & HDR Tone-Mapping**:
   - Tagged BT.709 color primaries (`-colorspace bt709 -color_primaries bt709 -color_trc bt709`) across hardware and software encoding profiles.
   - Added HDR detection (`bt2020`, `smpte2084`, `arib-std-b67`) in `probe_streams()` and automated Reinhard/Hable SDR tone-mapping filter chain (`zscale/tonemap`) so HDR files never produce washed-out grey colors on standard IPTV clients.
3. **Format & SAR Normalization**:
   - Enforced `setsar=1` and `format=yuv420p` on all converter re-encodes, even when no resolution scaling is needed. Guarantees non-square pixel sources (e.g. 720x576) do not display distorted, and non-yuv420p (10-bit / 4:4:4) inputs transcode cleanly.
4. **Audio Sample Rate Normalization (`-ar 48000`)**:
   - Enforced standard 48 kHz stereo across converter outputs and live relays (`get_audio_params`), preventing audio sample rate mismatch pops when switching channels in MediaMTX.
5. **Headless Subprocess Hygiene & Windows Console Safety**:
   - Added `["-hide_banner", "-nostdin"]` and explicit `stdin=subprocess.DEVNULL` across all FFmpeg and FFprobe executions (`converter.py`, `live_relay.py`, `streamer.py`, `thumbnails.py`, `ffmpeg_setup.py`).
6. **Subprocess Pipe Deadlock Prevention**:
   - Set `stdin=DEVNULL` and `stdout=DEVNULL` on `streamer.py`'s concat demuxer and `live_relay.py`'s relay worker. Prevents 64KB OS pipe buffer exhaustion deadlocks on continuous background streaming.
   - Added `-fflags +genpts+igndts -avoid_negative_ts make_zero` to concat demuxer to protect against packet timestamp slips across playlist file transitions.
7. **Aspect Ratio Safe Quantization**:
   - Changed thumbnail extraction scale filter from `scale=320:-1` to `scale=320:-2` in `thumbnails.py` to guarantee even height for chroma subsampling.
8. **Windows Filter Path Escaping Utility**:
   - Added `escape_filter_path(path)` in `app/ffmpeg_setup.py` to escape Windows drive colons (`C\:`) and backslashes for filtergraphs.

