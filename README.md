# 4K Screen Share

Cross-platform Python desktop app for peer-to-peer screen sharing with audio. The same executable launches both modes:

- `HOST`: share one monitor with optional system audio and microphone
- `JOIN`: connect to a host by IP address and 6-digit session PIN

The project uses `CustomTkinter` for the GUI, `mss` for screen capture, `sounddevice` for audio, `aiortc` for WebRTC transport, and a minimal TCP JSON signaling exchange for SDP offer/answer flow.

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
2. Pick the monitor, resolution, FPS, and quality preset.
3. Enable `Share system audio` and/or `Share microphone` if needed.
4. Click `Start Sharing`.
5. Give the viewer your local IP and 6-digit session PIN.
6. Click `Stop Sharing` to end the session cleanly.

## Join Mode

1. Click `Join a Session`.
2. Enter the host IP and 6-digit PIN.
3. Click `Connect`.
4. Use `Fullscreen` or press `F11` to toggle fullscreen mode.
5. Adjust the volume slider for received audio.

## Notes

- WebRTC is configured with Google STUN: `stun:stun.l.google.com:19302`.
- The signaling server listens on TCP port `8765` on the host machine.
- The packaged Windows app now carries its own `ffmpeg.exe` via `imageio-ffmpeg`, so NVENC does not depend on a separate FFmpeg install.
- On Windows and Linux, the host probes for NVIDIA GPUs and uses a bundled FFmpeg `h264_nvenc` packet pipeline automatically when available.
- On Windows with NVENC available, the `1080p/60` path now uses FFmpeg Desktop Duplication (`ddagrab`) for the actual stream source, so the reported FPS reflects the real stream pipeline instead of the lightweight preview capture loop.
- If an RTX GPU is detected but NVENC cannot be opened, the app falls back to a tuned `libx264` profile and shows a toast so the user knows why GPU offload is unavailable.
- The H.264 runtime is now tuned for real-time screen sharing: fixed GOPs, dynamic H.264 level selection, 60 FPS support, and lower-latency encoder presets.
- The accelerated NVENC path now avoids Python-side RGB conversion and resizing by passing raw `mss` BGRA frames straight into FFmpeg, which materially improves capture throughput on large desktops.
- System audio capture support varies by platform:
  - Windows works best with WASAPI loopback.
  - Linux usually needs a PulseAudio/PipeWire monitor device.
  - macOS often needs a virtual loopback device such as BlackHole.
- If the machine cannot sustain 4K capture, the host auto-downscales to 1080p and shows a non-blocking toast.
- The current `aiortc` / PyAV H.264 runtime is reliable through `1080p`. Requests above `1080p` are automatically downgraded to `1080p` with a toast so the session stays usable instead of connecting to a black screen.
- This project uses STUN only. Some NAT combinations still require a TURN server for reliable internet-wide connectivity.

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
