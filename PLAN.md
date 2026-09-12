# CommandCenter Feature Status & Optimization Plan

## Summary Status

| Feature / Task | Status | Implementation Details |
| :--- | :--- | :--- |
| **Feature 1: Stream Resolution Selection (720p vs 1080p)** | ✅ **Completed** | Implemented 24MB Named Pipe buffer, dynamic VBV & bitrate scaling, and UI selection. |
---

## Feature 1: Stream Resolution Selection (720p vs 1080p) [COMPLETED]

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
