# Warning - this app was AI Generated!
This app was created by an AI, it's fully safe to run but im just letting you know that its ai generated.
# 4K Screen Share

Cross-platform Python desktop app for peer-to-peer screen sharing with audio. The same executable launches both modes:

- `HOST`: share one monitor with optional system audio and microphone
- `JOIN`: connect by internet join code, or by host IP address and 6-digit session PIN

The project uses `CustomTkinter` for the GUI, `mss` for screen capture, `sounddevice` for audio, `aiortc` for WebRTC transport, direct TCP signaling for LAN/manual-forwarded sessions, and an automatic outbound signaling relay path for internet sessions.

## Project Layout

```text
screenshare/
├── main.py
├── gui/
│   ├── launcher.py
│   ├── host_view.py
│   └── viewer_view.py
├── capture/
│   ├── screen.py
│   └── audio.py
├── stream/
│   ├── encoder.py
│   ├── sender.py
│   └── receiver.py
├── network/
│   ├── signaling.py
│   └── session.py
├── utils/
│   └── resolution.py
├── requirements.txt
└── README.md
```

## Prerequisites

- Python `3.11+`
- PortAudio runtime for `sounddevice`

## Install

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Optional: if you want to override the bundled FFmpeg binary, make sure FFmpeg is installed and reachable from your shell:

```bash
ffmpeg -version
```

If FFmpeg is unavailable, the app now offers an automatic setup popup at startup when it can use the platform package manager.

4. Optional: override the built-in public TURN fallback with your own TURN service before launching the app.

Windows PowerShell:

```powershell
$env:SCREENSHARE_TURN_URLS="turn:your-turn-host:3478?transport=udp,turn:your-turn-host:3478?transport=tcp"
$env:SCREENSHARE_TURN_USERNAME="your-username"
$env:SCREENSHARE_TURN_CREDENTIAL="your-password"
```

macOS / Linux:

```bash
export SCREENSHARE_TURN_URLS="turn:your-turn-host:3478?transport=udp,turn:your-turn-host:3478?transport=tcp"
export SCREENSHARE_TURN_USERNAME="your-username"
export SCREENSHARE_TURN_CREDENTIAL="your-password"
```

5. Optional: provide your own public signaling relay URL. If you do not set one, the host now tries to publish an outbound public relay automatically.

Windows PowerShell:

```powershell
$env:SCREENSHARE_SIGNALING_RELAY_URL="https://your-relay.example.com"
```

macOS / Linux:

```bash
export SCREENSHARE_SIGNALING_RELAY_URL="https://your-relay.example.com"
```

## FFmpeg Install By OS

### Windows

1. Download a current FFmpeg build from `https://www.gyan.dev/ffmpeg/builds/` or `https://www.ffmpeg.org/download.html`.
2. Extract it, for example to `C:\ffmpeg`.
3. Add `C:\ffmpeg\bin` to your system `PATH`.
4. Open a new terminal and run `ffmpeg -version`.

### macOS

Install with Homebrew:

```bash
brew install ffmpeg
```

### Linux

Ubuntu / Debian:

```bash
sudo apt update
sudo apt install ffmpeg portaudio19-dev
```

Fedora:

```bash
sudo dnf install ffmpeg portaudio-devel
```

Arch:

```bash
sudo pacman -S ffmpeg portaudio
```

## Run The App

Start the application:

```bash
python -m screenshare.main
```

The launch screen presents the two modes.

## Host Mode

1. Click `Share My Screen`.
2. Pick the monitor, resolution, FPS, quality preset, and video encoder format (`H.264` or `H.265`).
3. Enable `Share system audio` and/or `Share microphone` if needed.
4. Click `Start Sharing`.
5. Share the `Internet Join Code` for internet sessions when the host shows it as ready, or share the local IP and 6-digit PIN for LAN sessions.
6. Click `Stop Sharing` to end the session cleanly.

## Join Mode

1. Click `Join a Session`.
2. Enter the `Internet Join Code` for public-network sessions, or enter the host IP and 6-digit PIN manually.
3. Click `Connect`.
4. Use `Fullscreen` or press `F11` to toggle fullscreen mode.
5. Adjust the volume slider for received audio.

## Notes

- WebRTC is configured with Google STUN by default: `stun:stun.l.google.com:19302`.
- If `SCREENSHARE_TURN_URLS` is set, the app adds your TURN relay servers to the ICE configuration.
- If `SCREENSHARE_TURN_URLS` is not set, the app falls back to a public TURN configuration so cross-network sessions still have a relay path for restrictive NATs. Set `SCREENSHARE_DISABLE_PUBLIC_TURN_FALLBACK=1` if you want to disable that fallback.
- The direct signaling server listens on TCP port `8765` on the host machine.
- The host now generates an `Internet Join Code` only when the internet signaling path is actually prepared.
- If `SCREENSHARE_SIGNALING_RELAY_URL` is set, the host and viewer use that public relay for the offer/answer exchange.
- If no relay URL is configured, the host starts an embedded localhost signaling relay and publishes it through an outbound Cloudflare Tunnel path when available, so internet sessions do not require inbound TCP to the host.
- If the automatic outbound relay cannot be prepared, the host falls back to direct UPnP router mapping for TCP `8765`.
- The app now refuses to publish a direct internet join code for non-global addresses such as CGNAT `100.64.0.0/10`, because those addresses are not publicly reachable.
- The packaged Windows app now carries its own `ffmpeg.exe` via `imageio-ffmpeg`, so NVENC does not depend on a separate FFmpeg install.
- On Windows, the app automatically downloads `cloudflared.exe` into `%LOCALAPPDATA%\\4KScreenShare\\tools` the first time it needs the outbound relay path.
- The Windows one-file build now shows an immediate splash screen while the bundled runtime extracts and the main UI initializes.
- If FFmpeg is missing entirely, startup now offers an install popup and uses WinGet, Homebrew, or the detected Linux package manager when available.
- On Windows and Linux, the host probes for NVIDIA GPUs and uses a bundled FFmpeg NVENC packet pipeline automatically when available.
- The host now exposes a `Video Encoder Format` selector with `H.264` and `H.265`.
- `H.265` / HEVC is fully wired into the in-app WebRTC transport through a project-owned codec registry and RTP integration layer, so future codecs such as `AV1` can be added without reworking the host and viewer flows again.
- On Windows with NVENC available, the `1080p/60` path now uses FFmpeg Desktop Duplication (`ddagrab`) for the actual stream source, so the reported FPS reflects the real stream pipeline instead of the lightweight preview capture loop.
- On RTX-class NVIDIA systems, selecting `H.265` uses `hevc_nvenc` for the live packet pipeline when the bundled FFmpeg runtime exposes it.
- If an RTX GPU is detected but NVENC cannot be opened for the selected format, the app falls back to a tuned software encoder (`libx264` or `libx265`) and shows a toast so the user knows why GPU offload is unavailable.
- The H.264 runtime is now tuned for real-time screen sharing: fixed GOPs, dynamic H.264 level selection, 60 FPS support, and lower-latency encoder presets.
- The HEVC runtime uses the same session flow, but allocates a larger receive jitter buffer because HEVC keyframes can span more RTP packets than aiortc's default video buffer allows.
- The accelerated NVENC path now avoids Python-side RGB conversion and resizing by passing raw `mss` BGRA frames straight into FFmpeg, which materially improves capture throughput on large desktops.
- System audio capture support varies by platform:
  - Windows works best with WASAPI loopback.
  - Linux usually needs a PulseAudio/PipeWire monitor device.
  - macOS often needs a virtual loopback device such as BlackHole.
- If the machine cannot sustain 4K capture, the host auto-downscales to 1080p and shows a non-blocking toast.
- The current `aiortc` / PyAV H.264 runtime is reliable through `1080p`. Requests above `1080p` are automatically downgraded to `1080p` with a toast so the session stays usable instead of connecting to a black screen.
- STUN-only media works for many public-network cases, but TURN is the reliable fallback when both sides are behind restrictive NATs or firewalls.

## Public Relay Server

If you want production-grade internet sessions without relying on router forwarding or Cloudflare Quick Tunnels, run the included signaling relay on any public machine:

```bash
python -m screenshare.network.relay_server --host 0.0.0.0 --port 8080
```

Then point both the host and the viewer at that relay before launching the app:

Windows PowerShell:

```powershell
$env:SCREENSHARE_SIGNALING_RELAY_URL="https://your-relay.example.com"
```

macOS / Linux:

```bash
export SCREENSHARE_SIGNALING_RELAY_URL="https://your-relay.example.com"
```

The join code then resolves to the relay session, while WebRTC still negotiates the actual media route over ICE. This is the preferred production path. Cloudflare Quick Tunnels are convenient for testing, but Cloudflare documents them as intended for testing and development only.

## Keyboard Shortcuts

- `Ctrl+Q` / `Cmd+Q`: quit the app from anywhere
- `F11`: fullscreen toggle in viewer mode

## Build A Single Windows `.exe`

This repo includes a PyInstaller spec and a PowerShell build script.

1. From the project root, run:

```powershell
.\build_windows.ps1
```

2. The packaged executable is written to:

```text
dist\4KScreenShare.exe
```

Notes:

- The build script creates `.venv-build` if needed.
- It installs runtime dependencies plus `pyinstaller`.
- The generated executable is Windows-only. Build on macOS/Linux separately for native binaries on those platforms.
