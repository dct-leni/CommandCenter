# PLAN_N100.md — Ubuntu Port for GMKtec G3 (Intel N100)

> Deep-tech plan for running CommandCenter on Ubuntu Server (GMKtec G3, Intel N100,
> SSH + remote Web UI). Written 2026-08-25. Return to this file when starting the port.

---

## 0. Executive summary

| Feature | On N100/Ubuntu | Effort |
|---|---|---|
| Converter (.ts) | ✅ works after encoder tuning (QSV/VAAPI) | S |
| Streamer (folders/slots/EPG/MediaMTX) | ✅ nearly as-is | S |
| Live HTTP relay (external URL → TS fanout) | ✅ with encoder tweak | S |
| Web streams (browser capture) | ❌ requires rework: Xvfb + x11grab + PipeWire | L |
| Remote UI / LAN viewing | ✅ already designed for it | none |

N100 capacity: ~2–4 concurrent 1080p30 hardware encodes (QSV/VAAPI) plus an
arbitrary number of copy-mode streams. RAM non-issue (copy streams ≈ 0 CPU).

---

## 1. Hardware/platform facts

- **CPU**: Intel N100 (Alder Lake-N) — 4× E-cores @ 3.4 GHz, TDP 6 W.
- **iGPU**: Intel UHD (12 EU…24 EU class) — Quick Sync: H.264/H.265 encode +
  decode, AV1 decode. Proven home-server transcoder class (Plex/Jellyfin).
- **OS target**: Ubuntu Server 24.04 LTS (PipeWire + WirePlumber default,
  intel-media-driver available, kernel ≥ 6.8).
- **Access model**: SSH for admin; Web UI from a LAN PC (server binds
  `0.0.0.0:8080`; existing `lan_only_guard` middleware already allows all
  private ranges — nothing to change).

### Encoder choice on N100
| Path | Use |
|---|---|
| Preferred HW encode | `h264_qsv` (oneVPL) **or** `h264_vaapi` (intel-media-driver) — decide by probing both at startup, keep winner |
| Preferred HW decode | VAAPI (`-hwaccel vaapi`) or QSV decode |
| Fallback | `libx264 -preset ultrafast` (≈1–1.5× realtime 1080p on 4 E-cores — last resort only) |

> Note: current `get_best_encoder()` probes nvenc → qsv → libx264. On Linux the
> qsv probe may succeed while the *params* differ (no `-rc cbr` semantics, needs
> `-global_quality`/ICQ or VBR). Plan assumes a small per-platform params table.

---

## 2. Windows-dependency inventory (what actually blocks Linux)

Per-module audit of the current codebase:

| Module | Win32 touchpoints | Portable today? |
|---|---|---|
| `config.py`, `epg.py`, `hls_cache.py` | none | ✅ 100% |
| `vpn_manager.py` | wireproxy (Go binary — Linux builds exist) | ✅ swap binary |
| `thumbnails.py` | `CREATE_NO_WINDOW` (already `os.name=="nt"`-guarded) | ✅ |
| `converter.py` | encoder names only (nvenc/qsv/libx264) | ⚠️ tune params |
| `streamer.py` | MediaMTX path under `bin/`; `CREATE_NO_WINDOW` guarded | ⚠️ swap MediaMTX binary |
| `ffmpeg_setup.py` | `is_qsv_available()` probe; encoder param tables assume Windows QSV flags | ⚠️ platform params table |
| `main.py` | `timeBeginPeriod`, `SetConsoleCtrlHandler`, taskkill tree-kill | ⚠️ guard blocks exist; need systemd unit instead |
| `live_relay.py` | **web branch**: gdigrab HWND input, dshow audio input, VB-Cable device lookup, HWND restore/minimize ctypes; **everything else** (loopback TCP output, client fanout, restart loop, logs) is portable | ⚠️ split web branch behind abstraction |
| `web_stream.py` | portapps/phyrox launcher + yml, `user32` ctypes (HWND lock/restore), window-title polling, policies.json layout, DETACHED_PROCESS | ❌ full rewrite of launch/capture layer |
| `audio_router.py` | pycaw/comtypes/WASAPI, SoundVolumeView.exe | ❌ replaced by PipeWire (see §5) |

`requirements.txt` already marks `comtypes`/`pycaw` as `sys_platform=="win32"`
— pip side is ready; code must import them lazily/guardedly (verify each import
site has an `os.name=="nt"` guard).

---

## 3. Architecture decision: one codebase, capture backends

Keep a single repo. Introduce a thin platform seam — **not** a fork:

```
app/
  platforms/
    __init__.py          # get_backend() -> "windows" | "linux" (os.name switch)
    base.py              # abstract interface (below)
    windows_impl.py      # existing behavior, moved not rewritten
    linux_impl.py        # new
```

Interface (only what actually differs):

```python
class CaptureBackend(Protocol):
    def build_web_video_input(self, session) -> list[str]: ...   # gdigrab hwnd | x11grab display
    def build_web_audio_input(self, session) -> list[str]: ...   # dshow CABLE | pulse sink.monitor
    def launch_browser(self, stream_id, url, proxy) -> BrowserSession: ...
    def close_browser(self, session) -> None: ...
    def best_encoder_params(self, mode) -> list[str]: ...        # nvenc | qsv/vaapi tables
```

`live_relay._auto_restart_loop` calls the backend; branch bodies shrink to
backend invocations. All Windows code paths stay byte-equivalent (regression
risk contained to the new Linux impl).

**Config addition** (optional, defaults fine): none required. Backend chosen by
OS. Keep `config.yml` schema identical so configs stay shareable.

---

## 4. Video capture on Linux — Xvfb + x11grab

### Design
- One **Xvfb display per web stream**, sized exactly to capture geometry:
  `Xvfb :{display_num} -screen 0 1280x720x24`
  → capture becomes trivial and pixel-exact:
  `-f x11grab -framerate 30 -video_size 1280x720 -draw_mouse 0 -i :{display_num}`
- No HWND hunting, no window-locking ctypes, no title polling — the display IS
  the window. The whole §15 HWND saga (launcher-PID mismatch, title fallbacks,
  minimize/restore) disappears on Linux.
- Display numbers allocated from a counter (e.g. 9900+stream_index), tracked in
  `WebStreamManager` like `bidi_ports` used to be.

### Browser
- Regular **Firefox ESR from mozilla.org tarball (NOT snap)** — snap confinement
  breaks Xvfb/env-var audio routing and profile dirs.
- Launch: `firefox -profile <dir> -width 1280 -height 720 --kiosk <url>` with
  `env DISPLAY=:{n} PULSE_SINK={sink_name} MOZ_DISABLE_CONTENT_SANDBOX=...`
  (sandbox flag only if profile-dir perms demand it).
- Profile generation: reuse `_create_firefox_profile()` minus Win-specific bits;
  keep `userContent.css` hide/reveal scheme verbatim (it is pure CSS — works
  identically); keep autoplay/DRM prefs (`media.eme.enabled=true` — Widevine
  works on Linux Firefox, typically ≤1080p).
- Kill: process-group kill (`start_new_sessions=True` + `os.killpg`) — replaces
  taskkill `/F /T`.

### Known trade-offs
- `x11grab` reads the framebuffer in userspace — slightly more CPU than gdigrab;
  at 1280×720@30 well within N100 budget (~a few % of one core per stream).
- No DWM-equivalent occlusion problems (Xvfb never occludes — the entire §10/§12
  freeze/slideshow battle does not apply).

---

## 5. Audio isolation on Linux — PipeWire (strictly better than the VB-Cable saga)

Per-stream **null-sink**, routed by environment variable, captured via its
monitor — true kernel-side isolation with zero external tools:

```bash
# per stream, at browser launch:
pactl load-module module-null-sink sink_name=cc_{stream_id} \
       sink_properties=device.description=CommandCenter_{stream_id}
# Firefox is launched with PULSE_SINK=cc_{stream_id}
#   → everything the page plays lands ONLY in this sink (process-level routing,
#     survives tab multiprocess — no PROCESS_LOOPBACK-style OS bugs possible)
```

FFmpeg audio input (native Pulse capture, no dshow equivalent needed):
```
-f pulse -i cc_{stream_id}.monitor -thread_queue_size 1024 -audio_buffer_size 20*
```
(*`-audio_buffer_size` is dshow-only — drop it; Pulse input uses
`-fragment_size` if latency tuning is ever needed.)

Teardown: `pactl unload-module <index>` (module index tracked alongside the
session). `restore_audio_routing()` concept vanishes — system output untouched
by construction.

This replaces: VB-Cable, SoundVolumeView, registry DefaultEndpoint overrides,
PROCESS_LOOPBACK research — the entire b5–b42 audio saga — with ~40 lines.

---

## 6. Encoder parameters — platform table in `ffmpeg_setup.py`

Add a platform dimension to the existing param builders:

```python
if os.name == "nt":   # existing nvenc/qsv tables — unchanged
else:                 # linux/N100
    ENCODER = probe(["h264_qsv", "h264_vaapi"])   # first that encodes 1 test frame
    # qsv:  -c:v h264_qsv -preset medium -global_quality 23 -look_ahead 0
    #        -b:v 2.8M -maxrate 2.8M -bufsize 6.4M -g 60
    # vaapi: -vaapi_device /dev/dri/renderD128 -c:v h264_vaapi -quality 23
    #        -rc_mode CBR -b:v 2.8M -g 60  (+ -hwaccel vaapi on inputs that benefit)
```

- Probe once at startup (`get_best_encoder()` extension), cache result.
- Keep bitrates/GOP identical (§3 rules hold: 2.8 Mbps cap, `-g 60`).
- Converter mode may use slightly higher quality (`global_quality 22`) since it
  is offline; live modes keep CBR/Capped-VBR for stability.

---

## 7. Process management & deployment

| Windows mechanism | Linux replacement |
|---|---|
| `taskkill /F /T` (`_kill_all_children`) | process groups: spawn children with `start_new_session=True`, kill via `os.killpg(SIGTERM→SIGKILL)`; keep atexit hook |
| `SetConsoleCtrlHandler` | unnecessary — systemd handles signals; drop on Linux |
| `timeBeginPeriod(1)` | unnecessary (Linux timer granularity fine); skip |
| `start.bat` | `run.sh` (venv activate + `python -m app.main`) |
| Auto-start Run-key toggle | **systemd user unit** `commandcenter.service`; the existing header toggle flips `server.auto_start` → runs `systemctl --user enable/disable` (Linux branch in `autostart.py`). Visible-window requirement doesn't apply on a headless server |
| `setup_binaries.bat` | new `setup_binaries.sh`: FFmpeg static Linux build (BtbN publishes linux64 gpl builds) → `bin/`, MediaMTX linux amd64 → `bin/`, wireproxy linux → `bin/`, Firefox ESR tarball → `bin/firefox/`, `apt install xvfb pulseaudio-utils intel-media-va-driver-non-free libmfx-gen1.2` |

MediaMTX: single YAML config per port logic is unchanged; binary name differs
(`mediamtx` not `mediamtx.exe`) — make `get_mediamtx_path()` platform-aware.

---

## 8. Implementation phases

### Phase A — foundation (S)
1. `platforms/` package + `get_backend()`; move nothing yet — just introduce the seam.
2. Guard every remaining bare `import comtypes/pycaw/winreg/ctypes.windll`
   behind `os.name == "nt"` (audit: `audio_router`, `web_stream`, `autostart`,
   `main`, `live_relay`).
3. `setup_binaries.sh` + `run.sh`; platform-aware binary paths in `ffmpeg_setup`.
4. Acceptance: app boots on Ubuntu; converter panel scans/probes; UI loads remotely.

### Phase B — converter + relay + streamer (S)
5. Linux encoder table + dual-probe (qsv → vaapi → libx264); verify a real
   conversion produces valid `.ts` (ffprobe checks codec/bitrate).
6. Streamer end-to-end: slot assign, MediaMTX RTMP+HLS, EPG generation
   (pure-python, expect zero changes).
7. Live HTTP relay: external URL → TS → VLC over LAN; confirm QSV encode path
   and client fanout.
8. Acceptance: 3 concurrent relays + 1 conversion on N100 without frame drops
   (`ffmpeg` logs fps steady 30; CPU < 60%).

### Phase C — web streams (L, the real work)
9. `linux_impl.py`: Xvfb lifecycle, Firefox launch (profile reuse), PipeWire
   null-sink create/route/unload, x11grab+pulse input args.
10. Rewire `live_relay` web branch through the backend interface (Windows path
    delegates to moved-but-unchanged windows impl).
11. Port `close/purge` semantics: killpg firefox, unload sink module, stop
    Xvfb, rmtree profile.
12. Wire thumbnail snapshots (existing 10-min task) against x11grab single-frame.
13. Acceptance: two simultaneous web streams with different pages → each
    recording contains ONLY its own page audio (PipeWire monitor check),
    zero speaker leakage; Widevine page plays and captures.

### Phase D — ops polish (XS)
14. `commandcenter.service` user unit + toggle wiring in `autostart.py` (Linux branch).
15. README/PLAN updates; smoke script `scripts/n100_smoke.sh` (boots app, scans,
    converts sample clip, starts relay, asserts HLS fetch 200).

Estimates (focused work): A: ½ day · B: 1 day · C: 2–3 days · D: ½ day.

---

## 9. Risks / gotchas

- **QSV-on-Linux flag drift**: `-rc cbr` etc. differ from Windows QSV builds;
  mitigate with the dual-probe + dedicated Linux param table (§6), verified by
  decoding the output bitrate curve.
- **Snap Firefox**: hard-broken for this use case — always mozilla.org tarball.
- **PipeWire permissions**: SSH sessions need `XDG_RUNTIME_DIR` set to the
  user's runtime dir for pactl/pipewire access; run the app as a login user
  (systemd **user** service with `loginctl enable-linger`), not root.
- **DRM ceiling**: Linux Widevine caps some services below 1080p; capture-based
  approach still works (it records rendered pixels) but source resolution may
  be lower than on Windows for certain providers.
- **x11grab color/tearing**: force 24-bit Xvfb depth; if tearing appears,
  add `-rtbufsize`-equivalent... not applicable; instead raise Xvfb to 60 Hz
  refresh and let FFmpeg pick 30 — x11grab duplicates frames cleanly.
- **wireproxy/VPN**: works, but MTU/TUN on unprivileged users needs
  `CAP_NET_ADMIN` → run wireproxy with `allowed_ips` limited or grant the
  binary `setcap cap_net_admin+ep` (document in setup script).
- **Do NOT port** `audio_loopback`-style experiments — PipeWire sinks make the
  entire PROCESS_LOOPBACK question moot on Linux.

## 10. Explicitly out of scope
- Windows feature regression: Windows path must stay byte-compatible (guarded
  by keeping windows impl moved-not-rewritten).
- GPU transcoding beyond iGPU (no dGPU expected on G3).
- Multi-user auth (LAN-trust model unchanged).
