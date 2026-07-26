# CommandCenter — Audio+Video Web Stream Capture Analysis

## User Need (Restated)

Capture **audio + video** from a visible browser window during a live relay stream.

### Requirements
- **Visible browser window** — user must log in and select video to play
- **Audio isolation** — browser audio must NOT play through PC speakers, stream only
- **No physical devices** — no capture cards, no external mics, no VB-Cable
- **No global PC setting changes** — no Stereo Mix, no driver installs
- **Extensions allowed**
- **Browser type not mandatory** (Chrome used)
- **Python modules allowed**
- **Stream must coexist with normal PC use** (watching own movies, etc.)

---

## GEMINI.md vs Actual Codebase — Staleness Analysis

`GEMINI.md` is **outdated**. The codebase evolved past it.

| Topic | GEMINI.md Says | Actual Code (Dec 2025) |
|-------|---------------|------------------------|
| Browser launch | `--app=URL` raw launch | **Selenium ChromeDriver** with `--app=URL` |
| Audio capture | References extension (`TabAudioCapture`) | **JS injection** via `driver.execute_script()` — `AUDIO_CAPTURE_JS` |
| Audio relay | TCP audio server (old `audio_capture.py`) | **WebSocket** server in `live_relay.py` + `feed_audio()` to FFmpeg stdin |
| Crop filter | `crop=iw:ih-38:0:38` | Same (still correct for `--app` window title bar) |
| Audio codec | References `audio_capture.py` class | No `audio_capture.py` exists — removed |
| Extension directory | `static/audio-relay-extension/` | **No extension directory exists** — MV2 extension abandoned |

**Files that exist currently** (no `audio_capture.py`, no extension):
- `app/web_stream.py` — Selenium browser manager + `AUDIO_CAPTURE_JS` inline
- `app/live_relay.py` — WebSocket audio server + FFmpeg pipe:0 audio input
- `app/ffmpeg_setup.py` — encoder params

**Files that DON'T exist anymore** (removed or never created):
- `app/audio_capture.py` — removed (WASAPI loopback TCP approach abandoned)
- `static/audio-relay-extension/` — removed (MV2 extension blocked in Chrome 150)
- `static/audio-relay-extension/manifest.json`, `background.js` — removed

---

## Current Architecture (The Only Implementation)

### Flow
```
[User clicks "Start"] → Stage 1: Selenium launches Chrome with --app=URL
                         → user logs in, navigates, plays video

[User clicks "Stream"] → Stage 2:
                          1) `inject_audio_capture()` runs AUDIO_CAPTURE_JS in browser
                          2) JS: video.captureStream() → AudioContext → ScriptProcessorNode
                          3) JS: PCM s16le 48kHz 2ch → WebSocket
                          4) WebSocket handler puts chunks into asyncio.Queue
                          5) audio WS server starts on random port (127.0.0.1)
                          6) FFmpeg starts with:
                             -f gdigrab -i title=WINDOW (video)
                             -f s16le -ac 2 -ar 48000 -i pipe:0 (audio)
                          7) feed_audio() reads from Queue → writes to process.stdin
                          8) Output: MPEG-TS → TCP loopback → HTTP broadcaster
```

### Audio Isolation **Claim**
- JS sets `video.muted = true` (video element muted)
- JS sets `mute.gain.value = 0` (AudioContext gain node at 0)
- Audio is captured BEFORE reaching speakers
- **ISOLATION ACHIEVED** — browser audio never hits speakers

### Files Involved
- `app/web_stream.py:24-64` — `AUDIO_CAPTURE_JS` (the JS injected)
- `app/web_stream.py:381-394` — `inject_audio_capture()` method
- `app/live_relay.py:534-683` — audio WS server + feed_audio() task
- `app/live_relay.py:568-578` — FFmpeg command building (gdigrab + pipe:0)

---

## Why Audio Might NOT Be Working — Root Cause Analysis

### 1. `captureStream()` May Be Silent on Cross-Origin Video
- If the page embeds a `<video>` with cross-origin src (CDN, different domain), `captureStream()` may return empty audio track
- **Chrome behavior**: `captureStream()` on cross-origin video returns **no audio track** — security restriction
- **YouTube behavior**: YouTube uses `<video>` on same origin (youtube.com), but uses MSE + EME — `captureStream()` may still lose audio
- **Fix needed**: Check `stream.getAudioTracks().length` — code already does this and sends `'no_audio_track'` error

### 2. Timing — JS Injected Before Video Exists
- User clicks "Stream" → JS injected immediately → page may not have `<video>` yet
- Code checks `document.querySelector('video')` — if no video, returns `'no_video_element'`
- **User must already be ON the video page with video playing before clicking "Stream"**

### 3. FFmpeg `pipe:0` — No Initial Audio Data
- FFmpeg starts with `-f s16le -i pipe:0` and waits for audio data
- `feed_audio()` has 1-second timeout loop — no data = no write
- If audio WS client connects late (JS takes time), FFmpeg may get no audio for seconds
- **FFmpeg doesn't crash on empty stdin** (it waits), but stream has no audio until data arrives

### 4. ScriptProcessorNode Deprecated
- `createScriptProcessor()` is deprecated in Chrome but still works
- May cause audio glitches or dropouts in long-running sessions
- Replacement: `AudioWorklet` — more complex but stable

### 5. `getUserMedia` Restrictions in `--app` Mode
- `captureStream()` on `<video>` element has different behavior than `getUserMedia({chromeMediaSource:'tab'})`
- The latter is only available from extensions (MV3 offscreen documents)
- Current approach uses `captureStream()` which should work without extension

---

## Evaluation of ALL Possible Solutions

### ✅ Solution A: Selenium + captureStream() + gdigrab (CURRENT)

| Aspect | Status | Notes |
|--------|--------|-------|
| Video capture | ✅ Works | gdigrab captures fullscreen window content |
| Audio capture | ⚠️ Untested | Relies on `video.captureStream()` — needs verification |
| Audio isolation | ✅ | `video.muted=true`, `gain=0` — no speaker output |
| Visible window | ✅ | Selenium Chrome `--app=URL` window, 1280x720 |
| No physical devices | ✅ | Pure software |
| No global changes | ✅ | Nothing modified |
| Extensions | ✅ Allowed | Not needed — JS injected directly |
| User login | ✅ | User navigates in visible window |
| Browser play video | ✅ | `<video>` element found via `querySelector` |

**Known issue**: DRM/EME content may block `captureStream()` entirely (Netflix, some YouTube)

### ❌ Solution B: Chrome Extension MV3 + tabCapture + Offscreen Document

| Aspect | Status | Notes |
|--------|--------|-------|
| Implementation | ❌ **Doesn't exist** | Extension files removed — never built |
| Audio capture | 👍 Would work | `chrome.tabCapture.capture()` captures BEFORE output routing |
| Audio isolation | ✅ Perfect | tabCapture captures before speaker routing |
| Complexity | High | Offscreen document, service worker, messaging |
| Chrome policy | ⚠️ Risk | Chrome may block non-store extensions in future |

### ❌ Solution C: WASAPI Loopback (`pyaudiowpatch`)

| Aspect | Status | Notes |
|--------|--------|-------|
| System isolation | ❌ **Breaks constraint** | Captures ALL system audio |
| No global changes | ✅ | `pyaudiowpatch` already installed via pip |
| Implementation | ❌ Removed | Old `audio_capture.py` deleted |

### ❌ Solution D: FFmpeg DirectShow Stereo Mix

| Aspect | Status | Notes |
|--------|--------|-------|
| System changes | ❌ **Requires Stereo Mix enable** | Violates "no global PC setting changes" |
| Audio isolation | ❌ Captures ALL system audio | No isolation |
| Availability | ❌ Not on this system | No Stereo Mix device detected |

### ❌ Solution E: Virtual Audio Cable / VB-Cable

| Aspect | Status | Notes |
|--------|--------|-------|
| Physical device | ❌ **Virtual driver install required** | Violates "no devices/no driver install" |
| Audio isolation | ✅ Would work | Route browser audio to virtual cable, capture from it |
| Changes | ❌ Requires driver install + reboot | Immediate disqualification |

### ❌ Solution F: FFmpeg WASAPI loopback (BtbN build)

| Aspect | Status | Notes |
|--------|--------|-------|
| FFmpeg build | ❌ BtbN lacks `wasapi` | `Unknown input format: 'wasapi'` |
| Audio isolation | ❌ All system audio | No isolation |
| No changes | ⚠️ None needed | Would work IF FFmpeg had WASAPI, but doesn't |

---

## ONLY Viable Solution: Current Selenium + captureStream() (Solution A)

**All other solutions are disqualified** — either violate constraints or don't exist.

### What Must Be Verified/Tested

1. **`video.captureStream()` audio track availability** on target sites (YouTube, streaming platforms)
2. **WebSocket connectivity** from injected JS to Python WS server
3. **PCM flow**: JS → WebSocket → Queue → FFmpeg stdin
4. **DRM/EME content** — `captureStream()` returns no audio tracks for protected content
5. **Timing** — user must be on video page with playing video BEFORE clicking "Stream"

### Potential Fixes (if audio still doesn't work)

#### Fix 1: Periodic Re-Injection of JS
- Current injection happens once. If user navigates, JS is lost.
- Solution: Poll every N seconds via `execute_script()` to check if audio WS is still connected; re-inject if not.

#### Fix 2: Use `getUserMedia({audio: true, video: true})` as Fallback
- Not available without extension. But could work with `chromeMediaSource: 'tab'` in some configurations.

#### Fix 3: Inject Into Every Page Navigation
- Use Selenium's `driver.get()` or listen for navigation events, re-inject JS on every page load.

#### Fix 4: Audio Worklet Instead of ScriptProcessorNode
- For long-running stability, replace `ScriptProcessorNode` with `AudioWorklet`.
- More complex but avoids deprecation warnings and potential dropouts.

---

## All Expectations Documented

| Expectation | Status |
|-------------|--------|
| Video captured via gdigrab | ✅ Working |
| Audio captured from browser tab | ⚠️ Architecture exists, needs verification |
| Audio isolated from PC speakers | ✅ `video.muted=true`, `gain=0` |
| Visible browser window for login | ✅ Selenium `--app=URL` |
| No capture cards / external mics | ✅ Pure software |
| No Stereo Mix / global changes | ✅ No system settings modified |
| No driver installations | ✅ No drivers needed |
| Extensions allowed | ✅ Not needed but allowed |
| Python-only additions | ✅ Everything in Python + JS |
| Stream alongside normal PC use | ✅ Only browser audio captured, not system-wide |
| User must navigate to video page | ✅ Stage 1 = browser_ready, user navigates and plays |
| One-click "Stream" after video is playing | ✅ Stage 2 injects JS, starts FFmpeg |
| HTTP broadcaster for multi-client | ✅ TCP loopback → HTTP broadcaster on port 1916 |

### What the User Must Do (Workflow)

1. Click **Start** → browser opens (Stage 1: `browser_ready`)
2. **In the browser**: log into platform, navigate to video, click play
3. Click **Stream** → JS injected, FFmpeg starts (Stage 2)
4. Stream goes live with audio+video

**Critical**: The video must be playing BEFORE clicking "Stream". The JS finds `<video>` element — if no video exists when Stage 2 triggers, audio capture fails silently (error logged: `'no_video_element'` or `'no_audio_track'`).

---

## Summary Table

| Solution | Audio Capture | Audio Isolation | No Changes | Works Now | Status |
|----------|--------------|----------------|------------|-----------|--------|
| A. Selenium + captureStream + gdigrab | captureStream() | `video.muted`, `gain=0` | ✅ | ⚠️ Untested | **CURRENT — verify** |
| B. Chrome MV3 extension + tabCapture | tabCapture API | Before output routing | ✅ | ❌ No code | Would work if built |
| C. WASAPI loopback (pyaudiowpatch) | Loopback | None (all system audio) | ✅ | ❌ Removed | ❌ No isolation |
| D. Stereo Mix DirectShow | Stereo Mix | None | ❌ Needs enable | ❌ No device | ❌ Disqualified |
| E. Virtual Audio Cable | Cable device | ✅ Route browser there | ❌ Driver install | ❌ Not installed | ❌ Disqualified |
| F. FFmpeg WASAPI BtbN | WASAPI loopback | None | ✅ | ❌ Build lacks | ❌ No wasapi in BtbN |

**Bottom line**: Only **Solution A** (current implementation) is viable without violating constraints. All others disqualified or don't exist. Audio needs verification — potential issues with DRM/cross-origin/captureStream behavior.
