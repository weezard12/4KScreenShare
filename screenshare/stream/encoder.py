from __future__ import annotations

import fractions
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, cast

try:
    import av
    from av.video.codeccontext import VideoCodecContext
except Exception:  # pragma: no cover - optional at import time
    av = None
    VideoCodecContext = object  # type: ignore[assignment]


TextCallback = Callable[[str], None]

_ACTIVE_RUNTIME_PROFILE: "H264RuntimeProfile | None" = None
_RUNTIME_MESSAGE_CALLBACK: TextCallback | None = None
_PATCHED_AIORTC_H264 = False
_EMITTED_MESSAGES: set[str] = set()


@dataclass(slots=True)
class GpuInfo:
    name: str
    vendor: str
    driver_version: str | None = None
    is_nvidia: bool = False
    is_rtx: bool = False


@dataclass(slots=True)
class EncoderSelection:
    ffmpeg_path: str | None
    codec: str
    transport_codec: str
    display_name: str
    hardware_accelerated: bool
    gpu_name: str | None = None
    runtime_codec_available: bool = False
    ffmpeg_codec_available: bool = False
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class QualityProfile:
    quality_name: str
    preset_name: str
    target_bitrate: str
    max_bitrate: str
    buffer_size: str
    tune: str


@dataclass(slots=True)
class H264RuntimeProfile:
    codec: str
    fallback_codec: str
    fps: int
    gop_size: int
    level: str
    bitrate_bps: int
    max_bitrate_bps: int
    options: dict[str, str]
    fallback_options: dict[str, str] = field(default_factory=dict)


def resolve_quality_profile(resolution_label: str, quality: str) -> QualityProfile:
    table = {
        "4K": {
            "High": ("22M", "25M", "30M", "film"),
            "Balanced": ("18M", "22M", "26M", "zerolatency"),
            "Low-latency": ("15M", "18M", "20M", "zerolatency"),
        },
        "1080p": {
            "High": ("8M", "9M", "10M", "film"),
            "Balanced": ("6.5M", "8M", "9M", "zerolatency"),
            "Low-latency": ("5M", "6M", "7M", "zerolatency"),
        },
        "720p": {
            "High": ("4M", "5M", "6M", "film"),
            "Balanced": ("3M", "4M", "5M", "zerolatency"),
            "Low-latency": ("2.2M", "3M", "4M", "zerolatency"),
        },
    }
    bitrate, maxrate, bufsize, tune = table.get(resolution_label, table["720p"])[quality]
    preset_name = "slow" if quality == "High" else "veryfast" if quality == "Balanced" else "ultrafast"
    return QualityProfile(
        quality_name=quality,
        preset_name=preset_name,
        target_bitrate=bitrate,
        max_bitrate=maxrate,
        buffer_size=bufsize,
        tune=tune,
    )


def detect_best_encoder(ffmpeg_path: str | None = None) -> EncoderSelection:
    ffmpeg_path = resolve_ffmpeg_binary(ffmpeg_path)
    av_codecs = _read_av_codecs()
    ffmpeg_encoders = _read_encoders(ffmpeg_path) if ffmpeg_path else set()
    gpu = detect_nvidia_gpu()

    system = platform.system().lower()

    notes: list[str] = []
    if gpu and gpu.is_nvidia and "h264_nvenc" in ffmpeg_encoders:
        label = "NVIDIA RTX NVENC H.264" if gpu.is_rtx else "NVIDIA NVENC H.264"
        if "h264_nvenc" not in av_codecs:
            notes.append(
                f"{gpu.name} detected. PyAV does not expose h264_nvenc, so the app will use the bundled FFmpeg NVENC pipeline."
            )
        return EncoderSelection(
            ffmpeg_path=ffmpeg_path,
            codec="h264_nvenc",
            transport_codec="video/H264",
            display_name=f"{label} ({gpu.name})",
            hardware_accelerated=True,
            gpu_name=gpu.name,
            runtime_codec_available="h264_nvenc" in av_codecs,
            ffmpeg_codec_available=True,
            notes=tuple(notes),
        )

    candidates: list[tuple[str, str]] = []
    if "windows" in system:
        candidates.extend([("h264_qsv", "Intel Quick Sync H.264")])
    elif "darwin" in system:
        candidates.extend([("h264_videotoolbox", "Apple VideoToolbox H.264")])
    elif "linux" in system:
        candidates.extend([("h264_vaapi", "VAAPI H.264")])

    for codec_name, label in candidates:
        if codec_name in av_codecs or codec_name in ffmpeg_encoders:
            return EncoderSelection(
                ffmpeg_path=ffmpeg_path,
                codec=codec_name,
                transport_codec="video/H264",
                display_name=label,
                hardware_accelerated=True,
                gpu_name=gpu.name if gpu else None,
                runtime_codec_available=codec_name in av_codecs,
                ffmpeg_codec_available=codec_name in ffmpeg_encoders,
                notes=tuple(notes),
            )

    if gpu and gpu.is_nvidia:
        notes.append(
            f"{gpu.name} detected, but bundled FFmpeg does not expose h264_nvenc on this system. Using tuned libx264."
        )

    return EncoderSelection(
        ffmpeg_path=ffmpeg_path,
        codec="libx264",
        transport_codec="video/H264",
        display_name=f"Tuned libx264 ({gpu.name})" if gpu else "Tuned libx264",
        hardware_accelerated=False,
        gpu_name=gpu.name if gpu else None,
        runtime_codec_available="libx264" in av_codecs,
        ffmpeg_codec_available="libx264" in ffmpeg_encoders,
        notes=tuple(notes),
    )


def build_h264_runtime_profile(
    selection: EncoderSelection,
    *,
    width: int,
    height: int,
    fps: int,
    quality_profile: QualityProfile,
) -> H264RuntimeProfile:
    bitrate_bps = parse_bitrate_to_bps(quality_profile.target_bitrate)
    max_bitrate_bps = parse_bitrate_to_bps(quality_profile.max_bitrate)
    # Use a 1-second GOP for faster decoder lock-on and quicker recovery after loss.
    gop_size = max(fps, 30)
    level = resolve_h264_level(width, height, fps)

    if selection.codec == "h264_nvenc":
        options = _build_nvenc_options(quality_profile, level, gop_size)
    else:
        options = _build_x264_options(quality_profile, level, gop_size, width, height)

    fallback_options = _build_x264_options(quality_profile, level, gop_size, width, height)

    return H264RuntimeProfile(
        codec=selection.codec,
        fallback_codec="libx264",
        fps=fps,
        gop_size=gop_size,
        level=level,
        bitrate_bps=bitrate_bps,
        max_bitrate_bps=max_bitrate_bps,
        options=options,
        fallback_options=fallback_options,
    )


def apply_aiortc_h264_profile(
    selection: EncoderSelection,
    *,
    width: int,
    height: int,
    fps: int,
    quality_profile: QualityProfile,
    on_message: TextCallback | None = None,
) -> H264RuntimeProfile:
    global _ACTIVE_RUNTIME_PROFILE, _RUNTIME_MESSAGE_CALLBACK, _PATCHED_AIORTC_H264

    profile = build_h264_runtime_profile(
        selection,
        width=width,
        height=height,
        fps=fps,
        quality_profile=quality_profile,
    )
    _ACTIVE_RUNTIME_PROFILE = profile
    _RUNTIME_MESSAGE_CALLBACK = on_message

    import aiortc.codecs.h264 as h264

    h264.MAX_FRAME_RATE = fps
    h264.DEFAULT_BITRATE = profile.bitrate_bps
    h264.MAX_BITRATE = max(profile.max_bitrate_bps, getattr(h264, "MAX_BITRATE", profile.max_bitrate_bps))

    if not _PATCHED_AIORTC_H264:
        original_create = h264.create_encoder_context

        def _patched_create_encoder_context(codec_name: str, frame_width: int, frame_height: int, bitrate: int):
            runtime = _ACTIVE_RUNTIME_PROFILE
            if runtime is None:
                return original_create(codec_name, frame_width, frame_height, bitrate)
            return _open_h264_codec_context(runtime, frame_width, frame_height, bitrate)

        h264.create_encoder_context = _patched_create_encoder_context  # type: ignore[assignment]
        _PATCHED_AIORTC_H264 = True

    if selection.notes:
        for note in selection.notes:
            _emit_once(note)

    if selection.hardware_accelerated and selection.gpu_name:
        _emit_once(f"{selection.gpu_name} detected. Streaming will prefer {selection.display_name}.")

    return profile


def build_ffmpeg_command(
    selection: EncoderSelection,
    *,
    width: int,
    height: int,
    fps: int,
    quality_profile: QualityProfile,
    output_target: str = "null",
) -> list[str]:
    ffmpeg_path = selection.ffmpeg_path or "ffmpeg"
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        selection.codec,
        "-b:v",
        quality_profile.target_bitrate,
        "-maxrate",
        quality_profile.max_bitrate,
        "-bufsize",
        quality_profile.buffer_size,
    ]
    if selection.codec == "h264_nvenc":
        runtime = build_h264_runtime_profile(
            selection,
            width=width,
            height=height,
            fps=fps,
            quality_profile=quality_profile,
        )
        for option_name, option_value in runtime.options.items():
            command.extend([f"-{option_name}", option_value])
    else:
        command.extend(["-preset", quality_profile.preset_name, "-tune", quality_profile.tune])
    command.extend(["-f", output_target, "-"])
    return command


def build_ffmpeg_packet_command(
    selection: EncoderSelection,
    *,
    input_width: int,
    input_height: int,
    output_width: int,
    output_height: int,
    fps: int,
    quality_profile: QualityProfile,
    input_pixel_format: str = "rgb24",
    output_target: str = "pipe:1",
) -> list[str]:
    ffmpeg_path = selection.ffmpeg_path or resolve_ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("FFmpeg is not available for the hardware packet pipeline.")

    runtime = build_h264_runtime_profile(
        selection,
        width=output_width,
        height=output_height,
        fps=fps,
        quality_profile=quality_profile,
    )

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-f",
        "rawvideo",
        "-pix_fmt",
        input_pixel_format,
        "-s",
        f"{input_width}x{input_height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
    ]

    if input_width != output_width or input_height != output_height:
        command.extend(
            [
                "-vf",
                f"scale={output_width}:{output_height}:flags=fast_bilinear",
            ]
        )

    command.extend(
        [
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        selection.codec,
        "-profile:v",
        "baseline",
        "-b:v",
        quality_profile.target_bitrate,
        "-maxrate:v",
        quality_profile.max_bitrate,
        "-bufsize:v",
        quality_profile.buffer_size,
        ]
    )

    if selection.codec == "h264_nvenc":
        command.extend(
            [
                "-preset",
                runtime.options["preset"],
                "-tune",
                runtime.options["tune"],
                "-rc",
                runtime.options["rc"],
                "-g",
                str(runtime.gop_size),
                "-bf",
                "0",
                "-forced-idr",
                "1",
                "-zerolatency",
                "1",
                "-spatial_aq",
                runtime.options["spatial_aq"],
                "-aq-strength",
                runtime.options["aq-strength"],
                "-temporal_aq",
                runtime.options["temporal_aq"],
                "-rc-lookahead",
                runtime.options["rc-lookahead"],
            ]
        )
    else:
        command.extend(
            [
                "-preset",
                runtime.options["preset"],
                "-tune",
                runtime.options["tune"],
                "-g",
                str(runtime.gop_size),
                "-keyint_min",
                str(runtime.gop_size),
                "-x264-params",
                runtime.options["x264-params"],
            ]
        )

    command.extend(
        [
            "-bsf:v",
            "dump_extra=freq=keyframe",
            "-f",
            "mpegts",
            "-mpegts_flags",
            "resend_headers",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-flush_packets",
            "1",
            output_target,
        ]
    )
    return command


class FFmpegEncoderPipeline:
    """Optional subprocess encoder that can be used for raw frame offloading."""

    def __init__(
        self,
        selection: EncoderSelection,
        *,
        width: int,
        height: int,
        fps: int,
        quality_profile: QualityProfile,
    ) -> None:
        self.command = build_ffmpeg_command(
            selection,
            width=width,
            height=height,
            fps=fps,
            quality_profile=quality_profile,
        )
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def encode_frame(self, frame_bytes: bytes) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("FFmpeg encoder process is not running.")
        self._process.stdin.write(frame_bytes)

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None


def resolve_ffmpeg_binary(preferred_path: str | None = None) -> str | None:
    if preferred_path and Path(preferred_path).exists():
        return preferred_path

    try:
        import imageio_ffmpeg

        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_path and Path(ffmpeg_path).exists():
            return ffmpeg_path
    except Exception:
        pass

    return shutil.which("ffmpeg")


def detect_nvidia_gpu() -> GpuInfo | None:
    gpu = _detect_gpu_with_nvidia_smi()
    if gpu is not None:
        return gpu

    system = platform.system().lower()
    if "windows" in system:
        gpu = _detect_gpu_with_windows_cim()
    elif "linux" in system:
        gpu = _detect_gpu_with_lspci()
    else:
        gpu = None
    return gpu


def parse_bitrate_to_bps(bitrate_str: str) -> int:
    br = bitrate_str.strip().upper()
    try:
        if br.endswith("M"):
            return int(float(br[:-1]) * 1_000_000)
        if br.endswith("K"):
            return int(float(br[:-1]) * 1_000)
        return int(br)
    except Exception:
        return 1_000_000


def resolve_h264_level(width: int, height: int, fps: int) -> str:
    pixels = width * height
    if pixels <= 1280 * 720:
        return "32" if fps > 30 else "31"
    if pixels <= 1920 * 1080:
        return "42" if fps > 30 else "40"
    return "52" if fps > 30 else "51"


def _build_nvenc_options(quality_profile: QualityProfile, level: str, gop_size: int) -> dict[str, str]:
    quality = quality_profile.quality_name
    preset = {"High": "p6", "Balanced": "p5", "Low-latency": "p3"}[quality]
    tune = {"High": "hq", "Balanced": "ll", "Low-latency": "ull"}[quality]
    rate_control = "vbr" if quality == "High" else "cbr"

    options = {
        "profile": "baseline",
        "level": level,
        "preset": preset,
        "tune": tune,
        "rc": rate_control,
        "g": str(gop_size),
        "bf": "0",
        "forced-idr": "1",
        "zerolatency": "1",
        "spatial_aq": "1",
        "aq-strength": "8" if quality == "High" else "6",
        "temporal_aq": "1" if quality != "Low-latency" else "0",
        "rc-lookahead": "8" if quality == "High" else "0",
    }
    return options


def _build_x264_options(
    quality_profile: QualityProfile,
    level: str,
    gop_size: int,
    width: int,
    height: int,
) -> dict[str, str]:
    preset = _resolve_realtime_x264_preset(quality_profile.quality_name, width, height)
    return {
        "profile": "baseline",
        "level": level,
        "preset": preset,
        "tune": "zerolatency",
        "x264-params": f"keyint={gop_size}:min-keyint={gop_size}:scenecut=0:force-cfr=1",
    }


def _resolve_realtime_x264_preset(quality: str, width: int, height: int) -> str:
    pixels = width * height
    if pixels >= 3840 * 2160:
        table = {"High": "veryfast", "Balanced": "superfast", "Low-latency": "ultrafast"}
    elif pixels >= 1920 * 1080:
        table = {"High": "faster", "Balanced": "veryfast", "Low-latency": "superfast"}
    else:
        table = {"High": "fast", "Balanced": "faster", "Low-latency": "veryfast"}
    return table[quality]


def _open_h264_codec_context(
    runtime: H264RuntimeProfile,
    width: int,
    height: int,
    bitrate: int,
) -> tuple[VideoCodecContext, bool]:
    if av is None:
        raise RuntimeError("PyAV is not available in this runtime.")

    candidates = [(runtime.codec, runtime.options)]
    if runtime.codec != runtime.fallback_codec:
        candidates.append((runtime.fallback_codec, runtime.fallback_options))

    last_error: Exception | None = None
    for codec_name, options in candidates:
        try:
            codec = cast(VideoCodecContext, av.CodecContext.create(codec_name, "w"))
            codec.width = width
            codec.height = height
            codec.bit_rate = max(250_000, min(bitrate, runtime.max_bitrate_bps))
            codec.pix_fmt = "yuv420p"
            codec.framerate = fractions.Fraction(runtime.fps, 1)
            codec.time_base = fractions.Fraction(1, runtime.fps)
            codec.gop_size = runtime.gop_size
            codec.options = dict(options)
            codec.open()
            if codec_name != runtime.codec:
                _emit_once(
                    f"{runtime.codec} could not be opened in this runtime. Falling back to {runtime.fallback_codec}."
                )
            return codec, False
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Unable to open an H.264 encoder context: {last_error}")


def _detect_gpu_with_nvidia_smi() -> GpuInfo | None:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if not parts:
            continue
        name = parts[0]
        if "NVIDIA" in name.upper() or "RTX" in name.upper():
            driver_version = parts[1] if len(parts) > 1 else None
            return GpuInfo(
                name=name,
                vendor="NVIDIA",
                driver_version=driver_version,
                is_nvidia=True,
                is_rtx="RTX" in name.upper(),
            )
    return None


def _detect_gpu_with_windows_cim() -> GpuInfo | None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        if "NVIDIA" in name.upper():
            return GpuInfo(
                name=name,
                vendor="NVIDIA",
                is_nvidia=True,
                is_rtx="RTX" in name.upper(),
            )
    return None


def _detect_gpu_with_lspci() -> GpuInfo | None:
    lspci = shutil.which("lspci")
    if not lspci:
        return None
    try:
        result = subprocess.run(
            [lspci],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        upper = line.upper()
        if "NVIDIA" not in upper:
            continue
        name = line.split(":", 2)[-1].strip()
        return GpuInfo(
            name=name,
            vendor="NVIDIA",
            is_nvidia=True,
            is_rtx="RTX" in upper,
        )
    return None


def _emit_once(message: str) -> None:
    if message in _EMITTED_MESSAGES:
        return
    _EMITTED_MESSAGES.add(message)
    if _RUNTIME_MESSAGE_CALLBACK is not None:
        _RUNTIME_MESSAGE_CALLBACK(message)


def _read_av_codecs() -> set[str]:
    if av is None:
        return set()
    try:
        codecs = getattr(av, "codecs_available", set())
        return set(codecs)
    except Exception:
        return set()


def _read_encoders(ffmpeg_path: str) -> set[str]:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
    except Exception:
        return set()
    return {
        line.split()[1]
        for line in result.stdout.splitlines()
        if line.startswith(" V") and len(line.split()) >= 2
    }
