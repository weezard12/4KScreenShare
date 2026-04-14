from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from aiortc import RTCRtpSender
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecCapability, RTCRtpCodecParameters

from screenshare.stream.hevc import H265Decoder, H265Encoder, h265_depayload


VIDEO_CODEC_H264 = "h264"
VIDEO_CODEC_H265 = "h265"


@dataclass(frozen=True)
class VideoCodecSpec:
    key: str
    label: str
    mime_type: str
    software_encoder: str
    nvidia_encoder: str
    platform_hardware_candidates: dict[str, tuple[tuple[str, str], ...]]


VIDEO_CODEC_SPECS: dict[str, VideoCodecSpec] = {
    VIDEO_CODEC_H264: VideoCodecSpec(
        key=VIDEO_CODEC_H264,
        label="H.264",
        mime_type="video/H264",
        software_encoder="libx264",
        nvidia_encoder="h264_nvenc",
        platform_hardware_candidates={
            "windows": (("h264_qsv", "Intel Quick Sync H.264"),),
            "darwin": (("h264_videotoolbox", "Apple VideoToolbox H.264"),),
            "linux": (("h264_vaapi", "VAAPI H.264"),),
        },
    ),
    VIDEO_CODEC_H265: VideoCodecSpec(
        key=VIDEO_CODEC_H265,
        label="H.265",
        mime_type="video/H265",
        software_encoder="libx265",
        nvidia_encoder="hevc_nvenc",
        platform_hardware_candidates={
            "windows": (("hevc_qsv", "Intel Quick Sync H.265"),),
            "darwin": (("hevc_videotoolbox", "Apple VideoToolbox H.265"),),
            "linux": (("hevc_vaapi", "VAAPI H.265"),),
        },
    ),
}

VIDEO_CODEC_LABELS = tuple(spec.label for spec in VIDEO_CODEC_SPECS.values())

_H265_REGISTERED = False


def normalize_video_codec(value: str | None) -> str:
    if not value:
        return VIDEO_CODEC_H264
    normalized = value.strip().lower()
    if normalized in VIDEO_CODEC_SPECS:
        return normalized
    for spec in VIDEO_CODEC_SPECS.values():
        if normalized == spec.label.lower():
            return spec.key
    raise ValueError(f"Unsupported video codec format: {value}")


def get_video_codec_spec(value: str | None) -> VideoCodecSpec:
    return VIDEO_CODEC_SPECS[normalize_video_codec(value)]


def preferred_video_capabilities(preferred_keys: Iterable[str], *, include_rtx: bool = True) -> list[RTCRtpCodecCapability]:
    ensure_webrtc_video_codecs_registered()
    capabilities = RTCRtpSender.getCapabilities("video").codecs
    selected: list[RTCRtpCodecCapability] = []
    seen: set[tuple[str, tuple[tuple[str, object], ...]]] = set()

    def _remember(codec: RTCRtpCodecCapability) -> None:
        marker = (
            codec.mimeType.lower(),
            tuple(sorted(codec.parameters.items())),
        )
        if marker in seen:
            return
        seen.add(marker)
        selected.append(codec)

    for key in preferred_keys:
        mime_type = get_video_codec_spec(key).mime_type.lower()
        for codec in capabilities:
            if codec.mimeType.lower() == mime_type:
                _remember(codec)

    if include_rtx:
        for codec in capabilities:
            if codec.mimeType.lower() == "video/rtx":
                _remember(codec)

    return selected


def ensure_webrtc_video_codecs_registered() -> None:
    global _H265_REGISTERED
    if _H265_REGISTERED:
        return

    import aiortc.codecs as codecs
    import aiortc.rtcrtpreceiver as rtcrtpreceiver
    import aiortc.rtcrtpsender as rtcrtpsender

    if not any(codec.mimeType.lower() == "video/h265" for codec in codecs.CODECS["video"]):
        payload_type = max(codec.payloadType for codec in codecs.CODECS["video"]) + 1
        codecs.CODECS["video"].extend(
            [
                RTCRtpCodecParameters(
                    mimeType="video/H265",
                    clockRate=90000,
                    payloadType=payload_type,
                    rtcpFeedback=[
                        RTCRtcpFeedback(type="nack"),
                        RTCRtcpFeedback(type="nack", parameter="pli"),
                        RTCRtcpFeedback(type="goog-remb"),
                    ],
                    parameters={},
                ),
                RTCRtpCodecParameters(
                    mimeType="video/rtx",
                    clockRate=90000,
                    payloadType=payload_type + 1,
                    parameters={"apt": payload_type},
                ),
            ]
        )

    original_get_encoder = codecs.get_encoder
    original_get_decoder = codecs.get_decoder
    original_depayload = codecs.depayload

    def _get_encoder(codec: RTCRtpCodecParameters):
        if codec.mimeType.lower() == "video/h265":
            return H265Encoder()
        return original_get_encoder(codec)

    def _get_decoder(codec: RTCRtpCodecParameters):
        if codec.mimeType.lower() == "video/h265":
            return H265Decoder()
        return original_get_decoder(codec)

    def _depayload(codec: RTCRtpCodecParameters, payload: bytes) -> bytes:
        if codec.mimeType.lower() == "video/h265":
            return h265_depayload(payload)
        return original_depayload(codec, payload)

    codecs.get_encoder = _get_encoder  # type: ignore[assignment]
    codecs.get_decoder = _get_decoder  # type: ignore[assignment]
    codecs.depayload = _depayload  # type: ignore[assignment]
    rtcrtpsender.get_encoder = _get_encoder  # type: ignore[assignment]
    rtcrtpreceiver.get_decoder = _get_decoder  # type: ignore[assignment]
    rtcrtpreceiver.depayload = _depayload  # type: ignore[assignment]

    _H265_REGISTERED = True


def tune_video_receiver(receiver: object, *, capacity: int = 512) -> None:
    from aiortc.jitterbuffer import JitterBuffer

    buffer_name = "_RTCRtpReceiver__jitter_buffer"
    current = getattr(receiver, buffer_name, None)
    if current is None or getattr(current, "capacity", 0) >= capacity:
        return
    setattr(receiver, buffer_name, JitterBuffer(capacity=capacity, is_video=True))
