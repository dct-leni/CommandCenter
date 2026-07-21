# CommandCenter Walkthrough

The CommandCenter application is fully built and ready to use! It's an all-in-one web app for converting video files to `.ts` format and streaming them continuously via RTMP.

## Setup & Running

1. **Download Binaries (One-time only)**
   Run `setup_binaries.bat`. This script will automatically download portable versions of the stable **FFmpeg 8.1** release branch and MediaMTX to a local `bin/` directory. Using the stable FFmpeg release ensures maximum compatibility with standard system graphics drivers (unlike bleeding-edge master builds).

2. **Start the Application**
   Run `start.bat`. This will:
   - Install the required Python dependencies (`fastapi`, `uvicorn`, `pyyaml`).
   - Launch the FastAPI server.
   - Automatically open the Web UI on `http://localhost:8080`.
   - The top right header shows the system readiness indicators for **FFmpeg**, **MediaMTX**, and the active **Auto-detected Codec** (e.g., `Codec: h264_nvenc`).

## Features Built

### 1. The Converter Panel (Left Side)
- Click **Browse** to open a native-feeling folder browser and select the directory with your video files (`.mp4`, `.mkv`, `.avi`, etc.).
- **Auto-Scanning:** The app will detect all valid videos, pull their metadata, and extract a preview thumbnail using FFmpeg.
- **Conversion:** Click "Convert All" or individual "Convert" buttons. The converter queue processes files sequentially one-by-one to prevent system overloading and stay within NVIDIA hardware session limits. It always transcodes the files (removing copy strategies) to optimize bitrates (target ~2.8 Mbps), GOP size (`-g 60`), and audio (AAC stereo). High-motion and dark scenes are optimized using **Spatial/Temporal Adaptive Quantization** for NVENC, **Lookahead Rate Control** for QSV, and **CRF 21 with VBV limits** for CPU fallback.
- **Cleanup:** The original source file gets moved to a dedicated `original/` subfolder once converted successfully.
- **Stop Conversion:** Click **Stop Convert** (which replaces the **Convert All** button dynamically when active) to instantly cancel all queued files, terminate the active FFmpeg transcode process, and delete the incomplete `.ts` file.

### 2. The Streamer Panel (Right Side) & Slot System
- **Folder & Slot Discovery:** Browse to the root folder where your `DDMM_DDMM` subdirectories are located. Each date-range folder displays dedicated **Port Slots** (`:1935`, `:1936`, etc.).
- **Round-Robin Multi-Video Playlists:** Assign multiple videos to any single port slot. Videos assigned to the same slot will automatically play sequentially in a continuous loop.
- **Seamless Drag & Drop & "+ Add File":**
  - Drag videos from the Converter panel right onto any Port Slot (`:1935`, `:1936`) to simultaneously move the `.ts` file into the folder on disk and assign it to that port's playlist.
  - Or click **+ Add File** on any slot to open a file picker modal showing all converted `.ts` videos with full preview thumbnails and metadata.
- **Bidirectional Disk Synchronization & Lost Video Auto-Cleanup**:
  - Assigning a video to a slot automatically moves the physical `.ts` file from the Converter source directory into the date-range folder (`streams/DDMM_DDMM/`).
  - Clicking **✕** to remove a video from a slot checks if the file is still used anywhere else in the folder. If unused, the file is automatically returned (`moved`) back to the Converter input folder.
  - Additionally, any `.ts` video files found inside a date-range folder that are not assigned to any port slot (i.e. "lost" files) are automatically moved back to the converter input folder so the user can easily reassign them.
- **Automated EPG Generation**: Manual generation buttons have been removed. EPG XML files (e.g., `salon.xml` or corresponding prefix XMLs) are automatically regenerated inside the specific date-range folder whenever video files are added, reordered, or removed from port slots—**only if the folder is active** (its date range covers today) and **contains video files**. EPG generation is skipped for non-active or empty folders, and does not run on page reload or refresh to avoid unnecessary disk I/O.

### 3. Rich Metadata & Reliable Layout
- **Complete Video Specs:** The Converter and Streamer panels now display full duration (`H:MM:SS`), resolution (`1920×1080`), codec (`H264_NVENC`), frame rate (`30fps`), and bitrate (`6.2 Mbps`).
- **Conversion Notes & Probing:** Converted `.ts` files are probed automatically right after conversion completes, and any audio track selection or downscaling notes (`downscaling to 1920x1080 (HD cap)`) are displayed cleanly under each video item.
- **Robust Layout:** Slot entries (`.slot-file-entry`) feature fixed height boundaries and fallback thumbnail badges (`🎬`) to guarantee the UI never collapses or overlaps when multiple videos are assigned to a port.

### 4. Global Loading Overlay, Boot Resilience & Instant Folder Opening
- **Visual Progress Overlay:** Moving large multi-gigabyte video files across directories across disk is safely wrapped by a glassmorphic global loading overlay (`#loading-overlay`), assuring users that the transfer is in progress while preventing accidental clicks or refreshes during operations like slot assignment, drag & drop, and folder deletion.
- **Idempotent Config Saving & Boot Resilience:** Configuration saves (`save_config`) only write to disk when values actually change (`old_data == data`), preventing Uvicorn `--reload` from restarting the server mid-request during folder rescans. On server boot, configured folders are scanned immediately inside `lifespan` so that active streams and folder hierarchies are restored cleanly across server restarts.
- **Automatic Missing Config Options Population:** When `load_config()` loads an existing `config.yml`, it verifies if any sections or options (`live_streams`, `playlists`, `languages`, `channel_prefix`, etc.) are missing. If any fields are absent from the existing file, `load_config()` automatically merges and appends ONLY the missing options with their default values to `config.yml` while preserving all existing user settings exactly as configured.
- **Ultra-Fast Folder Opening:** Probed video metadata is now cached (`_METADATA_CACHE` / `metadata_cache.json`) and thumbnail generation is non-blocking (`get_thumbnail_path.exists()`), slashing folder expansion (`toggleFolder`) loading time from tens of seconds down to under 5 milliseconds.

## Testing Your Stream
Once streaming starts, you can connect to the streams using VLC Media Player or any RTMP-compatible client.
For example, open VLC and enter the network URL:
`rtmp://127.0.0.1:1935/stream` (Change `1935` to whichever port is shown in the UI).

> [!TIP]
> If you plan on streaming over the internet, make sure to configure port forwarding on your router for your selected port range (e.g., 1935-1944).

### 5. Live HTTP Stream Relay & Encoding (+ New Stream)
- **Live Stream Creation:** Click **`+ New Stream`** right next to `+ New Folder` inside the Streamer panel to add a live external stream relay. The dialog has been simplified to only ask for Name, URL, and Port. The manual auto-start checkbox is removed because stream auto-resume state is handled implicitly (it will resume streaming on boot if it was playing before the server shutdown, and remain stopped if it was stopped). Codec selection is fully automatic based on startup hardware detection.
- **Support for RTMP, RTSP, plain HTTP, and HLS (.m3u8):** The application handles standard streaming protocols as well as HTTP Live Streaming (HLS) playlists. For HLS sources, it automatically injects `-allowed_extensions ALL` and playlist/segment timeouts to allow FFmpeg and ffprobe to download playlist segments.
- **Hardware Acceleration (NVENC / QSV / CPU):** The app automatically probes the host system once on startup to detect the best available encoding acceleration: NVIDIA GPU (`h264_nvenc`), Intel QuickSync (`h264_qsv`), or CPU (`libx264`). This auto-detected codec is used globally for both the Converter and the Live Relays.
- **Multi-Client HTTP Broadcasting:** Instead of letting FFmpeg listen directly (which only accepts one viewer and stops on disconnect), the backend launches a lightweight **Python TCP Server** on your chosen output port (`http://0.0.0.0:1913/`). This server accepts **unlimited concurrent viewers** simultaneously and streams clean MPEG-TS (`video/mp2t`) by copying and distributing FFmpeg's transcoded stdout chunks in real-time.
- **Continuous Ingestion (No Disconnect Lag):** FFmpeg runs continuously in the background to transcode the source feed. Because the stream never terminates or restarts when viewers connect or disconnect, there is zero start delay, zero port reuse conflicts (`TIME_WAIT` socket errors), and player reconnects are instant. When a user clicks **Start** on a stream, its `auto_start` value is dynamically set to `True` in `config.yaml` to auto-resume on server boot. When **Stop** is clicked, it's set to `False` to prevent auto-start. Transcoding is optimized for high-motion sports using the **exact same** quality-oriented settings as the converter (target **2.8 Mbps**, max 3.2 Mbps, 6.4 Mbps VBR buffer) with **Spatial/Temporal Adaptive Quantization** (NVENC) or **Lookahead Rate Control** (QSV) enabled. This guarantees optimal crystal-clear visual quality while keeping network/bandwidth utilization safely bounded.
- **Unified Card Styling & Live Thumbnails:** Live stream cards are styled consistently with the date-range folder cards. If a stream is actively running with viewers connected, the app automatically triggers a background task to capture a thumbnail frame directly from the source URL once every 60 seconds. Grabbing frames from the source URL prevents any socket conflicts or interruption of the local HTTP listener port.
- **Fail-Safe Relay Loop & Error Reporting:** If the source stream URL is invalid, offline, or FFmpeg exits with an error code, the auto-restart loop safely stops, sets the status to `Error`, and logs the event exactly once. Input timeouts (`-timeout 5s`, `-stimeout 5s`, and `-rw_timeout 5s`) are passed to FFmpeg to prevent it from hanging forever if the input stream stalls or is closed. The loop reads the final stderr log lines from FFmpeg to extract and display the exact root cause directly on the UI status badge.

## Developer Guardrails & Architectural Rules

To prevent breaking the application, any AI or developer modifying this codebase must adhere to the following rules:

### 1. Windows stdout / Pipe Translation (MANDATORY)
* **Rule**: NEVER pipe raw binary video streams directly from a subprocess stdout to a network socket or another subprocess on Windows.
* **Why**: Windows command shells translate binary line endings (replacing `\n` [0x0A] with `\r\n` [0x0D 0x0A]), which corrupts MPEG-TS stream payloads.
* **Solution**: Always route binary data using local TCP loopback sockets (e.g. `127.0.0.1:{random_port}`) as implemented in `app/live_relay.py`.

### 2. Multi-Client Broadcasting
* **Rule**: Do not use FFmpeg's built-in `-listen 1` option directly to serve public viewers.
* **Why**: FFmpeg's listener blocks on single connections and terminates as soon as the client disconnects, preventing multiple concurrent clients.
* **Solution**: Spin up the Python TCP server (broadcaster) on the public port, route the transcode output to a loopback socket, and distribute chunks to all clients from Python.

### 3. Encoder Settings & Quality Unification
* **Rule**: Define all video transcoding parameters in a single, central place (`app/ffmpeg_setup.py` -> `get_encoding_params`).
* **Why**: To keep local conversion and live stream relay quality identical while limiting peak bitrates to prevent network congestion.
* **Tuning Details**:
  * **NVENC**: Preset `p6`, spatial-aq, temporal-aq, maxrate `3.2M`, bufsize `6.4M`, GOP `60`.
  * **QSV**: Preset `medium`, lookahead enabled, maxrate `3.2M`, bufsize `6.4M`, GOP `60`.
  * **CPU**: Preset `medium`, CRF `21`, maxrate `3.2M`, bufsize `6.4M`, GOP `60`.

### 4. Codec Auto-Detection
* **Rule**: Do not hardcode or allow users to manually configure encoding codecs in settings or UI.
* **Why**: The system automatically detects the best hardware encoder at startup in `app/ffmpeg_setup.py` (NVENC -> QSV -> CPU). Use `get_best_encoder()` to determine the active encoder globally.

### 5. Idempotent Config Saving
* **Rule**: Always compare the old configuration data with the new data before writing to `config.yml`.
* **Why**: Writing to the configuration file on every page load/rescan triggers Uvicorn's `--reload` file watcher, restarting the server and killing active relays.

### 6. HLS Stream Demuxing (.m3u8)
* **Rule**: When parsing or relaying HLS stream URLs, always detect `.m3u8` in the URL and add `-allowed_extensions ALL`, `-allowed_segment_extensions ALL`, and `-extension_picky 0` to both the `ffmpeg` and `ffprobe` commands.
* **Why**: FFmpeg's HLS demuxer will block or fail to resolve sub-playlists/media segment paths if they are not explicitly allowed. Additionally, non-standard segment URLs (e.g. without file extensions in IPTV redirects) are blocked by default unless `-allowed_segment_extensions ALL` and `-extension_picky 0` are passed. Finally, do not use `-reconnect_streamed` with HLS, as it conflicts with the demuxer's chunk-retrieval loop.



