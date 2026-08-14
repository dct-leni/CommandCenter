# CommandCenter Walkthrough

CommandCenter app built. Video converter (.ts) + continuous RTMP streamer.

## Setup & Running

1. **Download Binaries (One-time)**
   Run `setup_binaries.bat`. Downloads portable **FFmpeg 8.1** + MediaMTX to `bin/`. Stable FFmpeg release ensures max GPU driver compatibility.

2. **Start Application**
   Run `start.bat`. Installs Python deps (`fastapi`, `uvicorn`, `pyyaml`), launches FastAPI server, opens UI on `http://localhost:8080`. Header shows readiness (**FFmpeg**, **MediaMTX**, **Auto-detected Codec** e.g. `Codec: h264_nvenc`).

## Features Built

### 1. Converter Panel (Left Side)
- Click **Browse** select video folder (`.mp4`, `.mkv`, `.avi`).
- **Auto-Scanning:** Detects videos, pulls metadata, extracts preview thumbnail via FFmpeg.
- **Conversion:** Click "Convert All" or individual "Convert". Processes sequentially. Transcodes to optimize bitrate (~2.8 Mbps), GOP (`-g 60`), audio (AAC stereo). High-motion/dark scenes optimized via **Spatial/Temporal AQ** (NVENC), **Lookahead** (QSV), **CRF 21 VBV** (CPU).
- **Cleanup:** Source file moves to `original/` subfolder post-conversion.
- **Stop Conversion:** Click **Stop Convert** to cancel queue, kill FFmpeg transcode, delete incomplete `.ts`.

### 2. Streamer Panel (Right Side) & Slot System
- **Folder & Slot Discovery:** Select root with `DDMM_DDMM` subdirectories. Shows **Port Slots** (`:1935`, `:1936`).
- **Round-Robin Multi-Video Playlists:** Assign videos to port slot. Plays sequentially in loop.
- **Drag & Drop & "+ Add File":**
  - Drag Converter videos to Port Slot (`:1935`, `:1936`) to move `.ts` file into stream dir + assign playlist.
  - Click **+ Add File** to select converted `.ts` videos.
- **Disk Sync & Lost Video Cleanup**:
  - Assign slot moves `.ts` to `streams/DDMM_DDMM/`.
  - Remove **✕** checks remaining usage. If unused, moves `.ts` back to Converter input.
  - Unassigned `.ts` files inside date-range folder auto-returned to input folder.
- **Automated EPG Generation**: Auto-regenerates EPG XML (`salon.xml`) inside active date-range folder when files change. Skipped if non-active or empty.

### 3. Rich Metadata & Reliable Layout
- **Video Specs:** Displays duration (`H:MM:SS`), resolution (`1920×1080`), codec (`H264_NVENC`), fps (`30fps`), bitrate (`6.2 Mbps`).
- **Notes & Probing:** Probes converted `.ts` files. Shows downscaling/audio notes under items.
- **Layout:** Slot entries (`.slot-file-entry`) fixed height + fallback thumbnail (`🎬`).

### 4. Global Loading Overlay, Boot Resilience & Instant Folder Opening
- **Progress Overlay:** Glassmorphic overlay (`#loading-overlay`) locks UI during multi-GB file transfers.
- **Idempotent Config Saving & Boot Resilience:** `save_config` writes only on change (`old_data == data`) to prevent Uvicorn reload loop. `lifespan` restores active streams on boot.
- **Missing Config Auto-Populate:** `load_config()` appends missing default keys (`live_streams`, `playlists`, etc.) to `config.yml` while preserving user values.
- **Ultra-Fast Folder Opening:** Metadata cached (`_METADATA_CACHE` / `metadata_cache.json`) + non-blocking thumbnails (`get_thumbnail_path.exists()`), dropping load time <5ms.

## Testing Stream
Connect VLC or RTMP client to `rtmp://127.0.0.1:1935/stream`.

> [!TIP]
> Port forward router for public stream range (1935-1944).

### 5. Live HTTP Stream Relay & Encoding (+ New Stream)
- **Live Stream Creation:** Click **`+ New Stream`** for external relays (Name, URL, Port). Auto-resumes based on previous status. Codec auto-detected.
- **Protocols:** RTMP, RTSP, plain HTTP, HLS (`.m3u8`). HLS injects `-allowed_extensions ALL` + timeouts.
- **Hardware Acceleration:** Auto-probes best encoder: NVENC (`h264_nvenc`) -> QSV (`h264_qsv`) -> CPU (`libx264`).
- **Multi-Client HTTP Broadcasting:** Python TCP Server (`http://0.0.0.0:1913/`) broadcasts MPEG-TS (`video/mp2t`) to unlimited concurrent clients.
- **Continuous Ingestion:** FFmpeg background transcode prevents disconnect delay / port reuse conflicts. Config auto-resume (`auto_start`). Transcodes high-motion content at 2.8 Mbps.
- **Card Styling & Live Thumbnails:** Streams show 60s live thumbnail snapshots from source URL.
- **Fail-Safe Relay & Error Logs:** Stops loop on stream failure, sets status `Error`, surfaces stderr root cause to UI.

## Developer Guardrails & Architectural Rules

### 1. Windows stdout / Pipe Translation (MANDATORY)
* **Rule**: NEVER pipe raw binary video streams directly from subprocess stdout on Windows.
* **Why**: Windows translates `\n` to `\r\n`, corrupting MPEG-TS.
* **Solution**: Route binary data via local TCP loopback (`127.0.0.1:{random_port}`) as in `app/live_relay.py`.

### 2. Multi-Client Broadcasting
* **Rule**: Do not use FFmpeg `-listen 1` directly for public viewers.
* **Why**: Blocks single connection, terminates on disconnect.
* **Solution**: Python TCP server on public port, FFmpeg transcode to loopback.

### 3. Unified Encoder Settings & Profile Isolation
* **Rule**: Centralize ALL transcode video & audio params in `app/ffmpeg_setup.py` (`get_encoding_params(encoder, mode="converter"|"relay"|"web")` & `get_audio_params(is_web)`).
* **Bitrate Cap**: Match `source_bitrate` 1:1 if <2.8 Mbps. Cap at target 2.8 Mbps, max 3.2 Mbps, buffer 6.4 Mbps for HD content.
* **Profiles**:
  * **Converter (`mode="converter"`)**: NVENC preset `p5`, VBR 2.8M, `spatial-aq 1`, `temporal-aq 1`, GOP `60`.
  * **Relay (`mode="relay"`)**: NVENC preset `p5`, VBR 2.8M, `temporal-aq 1`, GOP `60`.
  * **Web (`mode="web"`)**: GDIGrab window capture requires single-pass CBR (`-rc cbr -b:v 2.8M -maxrate 2.8M`) WITHOUT AQ or lookahead buffers to prevent GDI frame queue stalls. Audio uses `192k` AAC with `adelay=350|350`.

### 4. Multi-Web-Stream Audio Isolation (PROCESS_LOOPBACK Architecture)
* **Rule**: Multiple web streams (`stream_type == "web"`) run concurrently without audio crossover or speaker leakage.
* **Architecture**:
  * CommandCenter activates native Windows `PROCESS_LOOPBACK` (`IAudioClient` with `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK` and `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`) for each specific `firefox.exe` process tree.
  * Captures 16-bit 48kHz Stereo PCM buffers directly via dedicated lightweight tool `bin/app_loopback.exe` (compiled from `src/app_loopback.cpp`) and feeds directly into FFmpeg `stdin` (`-f s16le -ac 2 -ar 48000 -i pipe:0`).
  * 100% process-isolated: Zero virtual cables (`VB-Audio Virtual Cable` dropped), zero external routing tools (`SoundVolumeView.exe` dropped).
  * Keeps desktop speakers 100% silent while isolating each stream's audio completely.

### 5. Idempotent Config Saving
* **Rule**: Compare `old_data == data` before writing `config.yml` to prevent Uvicorn reload loops.
* **Locking**: `app/config.py` uses `threading.RLock()` to serialize config reads/writes across async handlers and WebSocket status loops. `update_config()` and `save_config()` both acquire this lock — `RLock` (reentrant) is MANDATORY because `update_config` internally calls `save_config`. NEVER downgrade to `threading.Lock()` or the same thread will deadlock on itself.
* **WebSocket Status Loop**: The `/ws/status` WebSocket calls `load_config()` → `get_all_status()` every second. Config save inside HTTP handlers (stop/start stream) runs synchronously inside `update_config()` → `save_config()` under the lock. Single-threaded asyncio event loop means no true concurrency, but `RLock` guards against future multi-threaded executors.

### 6. HLS Stream Demuxing (.m3u8)
* **Rule**: HLS URLs require `-allowed_extensions ALL`, `-allowed_segment_extensions ALL`, `-extension_picky 0`. Avoid `-reconnect_streamed`.

### 7. Keyframe Fast-Seeking for Thumbnails
* **Rule**: Place `-ss <seconds>` BEFORE `-i video_path` (`-ss 15.0`) for fast seeking (~15ms vs 2000ms).

### 8. Public Source Protection — No Hardcoded IPs/Domains
* **Rule**: NEVER hardcode real IPs, domains, or credentials in source code. Use `config.yml` + generic placeholders (`https://example.com/stream`, `rtmp://example.com/live`).

### 9. Web Stream Capture, HWND Lock & Browser Environment
* **Direct HWND Capture**: GDIGrab uses `-i hwnd=0x{hwnd:x}` targeting the top-level browser HWND. Avoids `-i title=...` which fails when pages dynamically update titles or contain Unicode emojis (`🥶🇩🇪`).
* **Process Tree Termination**: Call `psutil` or `taskkill /F /T /PID <proc_pid>` to terminate all background browser worker processes cleanly on stop.
* **Portable Firefox Policies, Extension & State-Aware Hover UI**:
  * Enterprise `policies.json` bypasses Terms of Use / First Run onboarding.
  * MV2 Extension (`content.js`) overrides Page Visibility API (`document.hidden`, `visibilityState`, `hasFocus`), preventing websites from auto-pausing video/audio when backgrounded.
  * **Native Header Auto-Hide & Hover Reveal (`userContent.css`)**:
    * Headers and navigation bars (`[class*='header']`, `[class*='menu']`, `header`, `nav`, `footer`) auto-hide smoothly (`opacity: 0 !important; transition: opacity 0.3s ease-in-out !important;`).
    * When the user moves or hovers the mouse over the top edge or header area, it immediately reveals (`opacity: 1 !important; pointer-events: auto !important;`) at `z-index: 100001` on top of the full-screen video container.
  * **Full-Window Video Scaling**: `#full-screen-closed` fills `100vw × 100vh` at `z-index: 99998` with `background: #000;`, eliminating top black bars without clipping or UI distortion.
* **FFmpeg Titlebar Filter**: `-vf "crop=iw:ih-38:0:38,format=yuv420p"` crops OS titlebar.

### 10. Web Stream Audio & COM Guardrails — FAILED Approaches (DO NOT RETRY)

These were tested and confirmed non-working. Do not suggest or re-implement them.

| Approach | Why it fails |
|---|---|
| DirectShow global capture from `CABLE Output` for multiple web streams | Mixes all audio playing to `CABLE Input` together; destroys multi-stream isolation. |
| Calling `ActivateAudioInterface` flat C export in `mmdevapi.dll` | Entry point does not exist in `mmdevapi.dll` (only `ActivateAudioInterfaceAsync` is exported). |
| `comtypes.CoInitialize()` (default STA) on asyncio event loop or background threads without a message pump | Causes cross-apartment WASAPI calls to deadlock the entire Uvicorn server. **Fix**: Always use `comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)` (MTA). |
| `CREATE_NO_WINDOW` on Firefox `subprocess.Popen` | Hides the window entirely → Firefox suspends all media. Never use on Firefox. Use `DETACHED_PROCESS` instead. |
| `SW_SHOWNOACTIVATE (4)` to un-minimize window | Does NOT restore minimized windows. Need `IsIconic()` check + `SW_RESTORE (9)`. |
| `dom.suspend_inactive.enabled: false` pref alone | Partially helps but Firefox still suspends GDI rendering when minimized to taskbar. Not sufficient alone without un-minimizing. |

### 11. Web Stream Audio — WORKING Approach (DRM Compatible & Multi-Stream Isolated)

**Working DRM-Compatible Audio Architecture:**
Uses native Windows `PROCESS_LOOPBACK` capture (`IAudioClient` with `AUDIOCLIENT_ACTIVATION_PARAMS` where `ActivationType = 1` and `ProcessLoopbackMode = 0` / Include Target Process Tree).
- Implemented via standalone helper binary `bin/app_loopback.exe` (`src/app_loopback.cpp`) managed by `app/audio_router.py`.
- Employs WASAPI `GetNextPacketSize` packet loop with 20ms silence filler on timeout, achieving 99.9% exact 1:1 real-time packet delivery.
- Captures audio for each stream's specific Firefox process tree.
- Audio packets (raw 16-bit PCM 48kHz stereo) are piped directly into FFmpeg `stdin` (`-f s16le -ac 2 -ar 48000 -i pipe:0`).
- Each web stream receives strictly its own isolated audio with zero crosstalk and zero external speaker playback.
- No virtual cables (`VB-Cable`) and no external tools (`SoundVolumeView.exe`) required.

### 12. Web Stream Video Capture & Smoothness — WHAT MADE THINGS WORSE vs BETTER

#### DO NOT RETRY (Made Video Worse / Caused Freezing / Slideshow / Gray Screen):
| Parameter / Approach | Impact & Why it failed |
|---|---|
| `-framerate 60` on GDIGrab | **Made Worse (Slideshow)**: Windows GDI BitBlt takes ~25ms per frame; 60fps (16.6ms budget) overloaded GDI, dropping >50% of frames and causing a stuttering slideshow. Keep `-framerate 30`. |
| `SetWindowPos(HWND_BOTTOM)` / `-32000, -32000` | **Made Worse (Freeze)**: Windows DWM marks windows at `HWND_BOTTOM` or off-screen as 100% occluded, dropping DWM frame rendering to 0 FPS and stalling `gdigrab`. |
| `-vsync cfr` / `-async 1` | **Made Worse (Latency/Lag)**: Added internal FFmpeg buffer delays that caused frame timing jitter. |
| Full GPU WebRender (`gfx.webrender.all: true`) | **Made Worse (Gray Screen)**: DirectComposition hardware overlays bypass GDI window DCs, resulting in a gray box in `gdigrab`. |
| Unbatched `writer.drain()` on 50-item Queue | **Made Worse (1-Sec Freeze)**: Calling `writer.drain()` on every 2.6KB chunk caused TCP socket backpressure, filling the 50-item queue and dropping 1s of video packets every ~1s. |
| `-probesize 32K` / `-analyzeduration 0` on GDIGrab | **Made Worse (Broken Pixels)**: Forces FFmpeg to encode before GDIGrab probes window dimensions, causing macroblock corruption. |
| NVENC `-delay 0` / `-rc-lookahead 0` / `-tune ll` | **Made Worse (Slideshow)**: Invalid/incompatible flags in FFmpeg 8.1 NVENC wrapper; broke NVENC pipeline causing severe frame drops. |
| `-vf fps=fps=30` | **Made Worse (Slideshow)**: Forced frame duplication on variable input framerates, desyncing GDIGrab and causing fast slideshow lag. |
| `-rtbufsize` on `-f gdigrab` | **Made Worse (Few FPS)**: `-rtbufsize` is a DirectShow-only flag; passing to GDIGrab corrupted memory allocation and dropped 90% of frames. |
| Removing `-use_wallclock_as_timestamps 1` from GDIGrab | **Made Worse (Larger Freezes)**: Caused A/V input clock drift between GDIGrab (system time) and DShow (hardware audio clock), causing multi-frame interleave stalls. |
| Setting GOP `-g 30` (1s keyframe interval) | **Made Worse (Stutter)**: Doubled I-frame bandwidth frequency, causing bitrate reservoir dips and periodic stutter every 30 frames. Keep `-g 60`. |
| NVENC preset `p2` without `spatial-aq` | **Made Worse (Slideshow)**: Caused NVENC pipeline frame drops and slideshow. Keep `p4` with `-spatial-aq 1`. |
| `reader.read(65536)` 64KB socket buffer | **No Effect (Micro-freezes persistent)**: Increasing socket read buffer to 64KB did not eliminate the 1-second micro-freeze. |
| `-bsf:v dump_extra` on `h264_nvenc` | **Made Worse (Keyframe Freeze)**: NVENC outputs Annex B SPS/PPS natively. `dump_extra` inserted duplicate SPS/PPS headers before keyframes, causing H.264 decoders to flush their Decoded Picture Buffer (DPB) and freeze every 1-2s. |
| Background `ffmpeg -i <HTML URL>` thumbnail probe | **Made Worse (15s Stream Freeze)**: Running FFmpeg directly against HTML web page URLs caused 15s CPU/Network lockups during demuxing. Skip thumbnail generation for web URLs. |
| `-isync 0` flag on GDIGrab + DirectShow | **Made Worse (No Stream Output)**: GDIGrab provides no container start_time. Passing `-isync 0` caused FFmpeg to output "Unable to identify start times for Inputs #1 and 0 both" and block stream output entirely. |
| `libx264` CPU encoding for web streams | **Made Worse (Single Frozen Image)**: Ultrafast zerolatency CPU encoding caused 100% video frame drops on GDIGrab window input. |

#### KEEP & ENFORCE (Made Video Better / Fixed Issues):
| Parameter / Approach | Why it Works |
|---|---|
| `-flush_packets 1` + `tcp_nodelay=1` | **Fixed Chunk Freezes**: Forces FFmpeg to flush MPEG-TS packets immediately per frame instead of buffering in 64KB chunks. |
| `reader.read(188 * 35)` (~6.5KB) | **Fixed Loopback Stalls**: Asyncio loopback socket reads small, smooth packet slices 30-60 times/sec. |
| Page Visibility API override in `content.js` | **Fixed Website Auto-Pause**: Overrides `document.hidden`, `visibilityState`, `hasFocus`, stopping TikTok/YouTube from pausing video/audio. |
| Software GDI rendering in Firefox | **Fixed Gray Screen**: `gfx.webrender.all: false`, `gfx.webrender.software: true`, `layers.acceleration.disabled: true` ensures GDI captures full color without WebRender fallback loops. |
| Occlusion & Efficiency Mode prefs | `widget.windows.window_occlusion_tracking.enabled: false`, `dom.ipc.processPriorityManager.backgroundUsesEcoQoS: false`, `network.http.throttle.enable: false` prevent Win11 throttling. |
| Windows 1ms Timer (`timeBeginPeriod(1)`) | **Fixed OS Quantum Slips**: Increases Windows timer interrupt resolution from 15.625ms to 1.000ms, eliminating 15.6ms GDIGrab `av_usleep()` timer slips every ~1s. |
| Zero-Hold Audio Interleaving (`-max_interleave_delta 0`) | **Fixed Video Holds**: Setting `-max_interleave_delta 0` for web streams flushes video packets instantly without interleave holds. |
| Low-Latency DirectShow Audio (`-audio_buffer_size 20` + `-isync 0`) | **Fixed Audio Latency**: Delivers 20ms audio chunks 50x/sec and slaves audio dynamically to GDIGrab's master wallclock. |
| Lip Sync Offset (`-af adelay=350|350...`) | **Fixed Lip Sync**: Delays audio by 350ms to match GDIGrab/NVENC processing pipeline latency in 1:1 lip sync. |
| Dynamic VB-Audio Resolution (`get_cable_device_ids()`) | **Universal Multi-PC Compatibility**: Resolves device names via WASAPI (`pycaw`) dynamically across different PCs instead of hardcoded strings. |

### 13. Audio/Video Clock & Interleave Synchronization

**Root Cause of Competing Master Wallclocks & Audio Holds**:
When `-use_wallclock_as_timestamps 1` was specified on BOTH Input 0 (GDIGrab) and Input 1 (DirectShow Audio), FFmpeg treated both inputs as competing master wallclock sources. Non-deterministic Windows OS thread scheduling caused the audio thread timestamp to occasionally lead the video thread timestamp, forcing FFmpeg's interleave queue to stall for ~33ms until the video thread caught up (**micro-freeze every ~1s**).

**Proven Architecture**:
1. Keep `-use_wallclock_as_timestamps 1` ONLY on Input 0 (GDIGrab) as the single master wallclock source.
2. Add `-isync 0` to Input 1 (DirectShow Audio) to slave audio timestamps dynamically to Input 0.
3. Pass `-audio_buffer_size 20` to DirectShow input for continuous 20ms audio slicing.
4. Pass `-max_interleave_delta 0` on output to flush video packets instantly without interleave queue holds.
5. Apply `-af adelay=350|350,aresample=async=1000:min_hard_comp=0.100000:first_pts=0` for 1:1 lip sync.

### 14. SOCKS5 Performance Bottlenecks & Optimization Rules

* **Handshake Latency**: SOCKS5 requires 3–4 round-trip handshakes (greeting, auth negotiation, domain request, server reply) before data payload transmission, adding 200–600ms latency per connection compared to 1 RTT HTTP `CONNECT`.
* **DNS Resolution Bottlenecks (`socks5://` vs `socks5h://`)**: `socks5://` forces local DNS lookup before proxying (causes slow timeouts if local DNS is blocked or throttled). `socks5h://` delegates DNS resolution to the remote proxy server.
* **Lack of Multiplexing**: SOCKS5 opens a separate TCP tunnel for every HTTP request/segment, causing connection creation queueing during media streaming.
* **SSH SOCKS Buffer Constraints**: Dynamic SSH SOCKS tunnels (`ssh -D`) use a fixed 2MB window buffer; high-bitrate video streams suffer TCP window exhaustion and severe bandwidth drops.
* **Optimization Strategy**: Convert SOCKS5 to HTTP proxy locally (`socks5h://` with remote DNS).
* **TCP Window BDP Scaling**: Set `SO_RCVBUF` / `SO_SNDBUF` to 2MB (`2,097,152` bytes) to match WAN BDP windows without TCP buffer exhaustion.
* **On-Demand SOCKS5 Connection Creation**: Avoid holding pre-authenticated idle SOCKS5 sockets in memory queues. Remote SOCKS servers drop idle connections after 5s, causing browser white pages and triggering remote rate-limits. Use on-demand connection creation with 2MB TCP BDP windows.
* **Asyncio High-Water Marks**: Python `asyncio` limits socket writes to 64KB by default. To unlock full 2MB OS TCP socket speeds, you MUST synchronize the asyncio transport buffers: `writer.transport.set_write_buffer_limits(high=2097152, low=1048576)`. Otherwise, `writer.drain()` causes heavy stuttering.

### 15. Browser Window HWND Capture & Cleanup (MANDATORY)

* **Rule**: `start_browser_for_stream()` MUST call `wait_for_window_title()` after `launch_browser()` to capture the actual Firefox window HWND.
* **Why**: `launch_browser()` stores `subprocess.Popen.pid` which (for phyrox-portable) is the **launcher** PID, not the actual `firefox.exe` PID. The phyrox-portable launcher exits immediately after spawning Firefox. Calling `taskkill /F /T /PID <launcher_pid>` kills nothing because the launcher is already gone, leaving Firefox orphaned.
* **Solution**:
  1. `start_browser_for_stream()` calls `wait_for_window_title(stream_id, name, url, timeout=10.0)` via `asyncio.to_thread()` after `launch_browser()`.
  2. `wait_for_window_title()` resolves the actual HWND via `EnumWindows` + PID scan, stores it in `web_stream_manager.window_hwnds[stream_id]`.
  3. `close_browser()` retrieves the HWND, calls `GetWindowThreadProcessId(hwnd)` to get the actual `firefox.exe` PID, then `taskkill /F /T /PID <firefox_pid>`.
* **Fallback**: If HWND is `None`, `close_browser()` falls back to killing by launcher PID — but launcher may have already exited. Always ensure HWND capture succeeds.

### 16. Pure Python Audio Capture & Teardown
* **Rule**: Audio capture uses pure Python WASAPI `PROCESS_LOOPBACK` worker threads (`ProcessLoopbackAudioCapture`).
* **Teardown**: On stream stop, `thread.stop()` signals `kernel32.SetEvent` to wake up the thread immediately and close COM handles and OS pipe descriptors cleanly. No external audio router subprocesses are invoked.

### 17. Async Safety — No `time.sleep()` in Coroutines

* **Rule**: NEVER call `time.sleep()` inside async coroutines. Use `await asyncio.sleep()`.
* **Why**: `time.sleep()` blocks the entire event loop thread. `await asyncio.sleep()` yields control.
* **Affected**: Window-restore block in `_auto_restart_loop` (`live_relay.py`) — uses `await asyncio.sleep(0.3)`.

### 18. Windows COM Apartment Model & Deadlock Prevention (MANDATORY)

* **Rule**: ALWAYS initialize COM on worker threads and background tasks with `comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)` (MTA).
* **Why**: Python's `comtypes.CoInitialize()` defaults to Single-Threaded Apartment (STA). The asyncio event loop and thread-pool workers do not run a Windows message pump (`GetMessage`/`DispatchMessage`). Calling cross-apartment WASAPI COM methods on an STA thread without a message pump deadlocks the entire Uvicorn process.
* **Solution**: Wrap all COM device queries with `_com_init()` / `_com_uninit()` using `COINIT_MULTITHREADED` and run them via `asyncio.to_thread` / `run_in_executor`.

### 19. Background Task Error Trapping

* **Rule**: NEVER spawn fire-and-forget `asyncio.create_task()` without exception callbacks.
* **Why**: Unhandled exceptions in background asyncio tasks fail silently or crash the event loop.
* **Solution**: Use `safe_create_task(coro, name=...)` in `app/main.py` which automatically logs tracebacks upon task failure.


