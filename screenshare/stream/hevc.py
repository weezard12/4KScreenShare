from __future__ import annotations

import fractions
import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional, Tuple, cast

import av
from av.frame import Frame
from av.packet import Packet
from av.video.codeccontext import VideoCodecContext

from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import VIDEO_TIME_BASE, convert_timebase
from aiortc.codecs.base import Decoder, Encoder


logger = logging.getLogger(__name__)

TextCallback = Callable[[str], None]

DEFAULT_BITRATE = 1_000_000
MIN_BITRATE = 500_000
MAX_BITRATE = 25_000_000
PACKET_MAX = 1300

HEVC_NAL_TYPE_AP = 48
HEVC_NAL_TYPE_FU = 49
HEVC_HEADER_SIZE = 2
HEVC_FU_HEADER_SIZE = 3

_EMITTED_MESSAGES: set[str] = set()
_RUNTIME_MESSAGE_CALLBACK: TextCallback | None = None


@dataclass(slots=True)
class H265RuntimeConfig:
    codec: str
    fallback_codec: str
    fps: int
    gop_size: int
    bitrate_bps: int
    max_bitrate_bps: int
    options: dict[str, str]
    fallback_options: dict[str, str] = field(default_factory=dict)


_ACTIVE_RUNTIME_CONFIG = H265RuntimeConfig(
    codec="libx265",
    fallback_codec="libx265",
    fps=30,
    gop_size=30,
    bitrate_bps=DEFAULT_BITRATE,
    max_bitrate_bps=DEFAULT_BITRATE * 2,
    options={
        "preset": "superfast",
        "tune": "zerolatency",
        "x265-params": "repeat-headers=1:keyint=30:min-keyint=30:scenecut=0:bframes=0:no-open-gop=1",
    },
    fallback_options={
        "preset": "superfast",
        "tune": "zerolatency",
        "x265-params": "repeat-headers=1:keyint=30:min-keyint=30:scenecut=0:bframes=0:no-open-gop=1",
    },
)


class H265PayloadDescriptor:
    def __init__(self, first_fragment: bool) -> None:
        self.first_fragment = first_fragment

    @classmethod
    def parse(cls, data: bytes) -> Tuple["H265PayloadDescriptor", bytes]:
        if len(data) < HEVC_HEADER_SIZE:
            raise ValueError("HEVC NAL unit is too short")

        nal_type = (data[0] >> 1) & 0x3F
        if nal_type < HEVC_NAL_TYPE_AP:
            return cls(first_fragment=True), b"\x00\x00\x00\x01" + data

        if nal_type == HEVC_NAL_TYPE_FU:
            if len(data) < HEVC_FU_HEADER_SIZE + 1:
                raise ValueError("HEVC FU payload is truncated")
            fu_header = data[2]
            first_fragment = bool(fu_header & 0x80)
            original_nal_type = fu_header & 0x3F
            original_header = bytes(
                [
                    (data[0] & 0x81) | (original_nal_type << 1),
                    data[1],
                ]
            )
            output = b""
            if first_fragment:
                output += b"\x00\x00\x00\x01" + original_header
            output += data[3:]
            return cls(first_fragment=first_fragment), output

        raise ValueError(f"HEVC NAL unit type {nal_type} is not supported")


class H265Decoder(Decoder):
    def __init__(self) -> None:
        self.codec = cast(VideoCodecContext, av.CodecContext.create("hevc", "r"))

    def decode(self, encoded_frame: JitterFrame) -> List[Frame]:
        try:
            packet = av.Packet(encoded_frame.data)
            packet.pts = encoded_frame.timestamp
            packet.time_base = VIDEO_TIME_BASE
            return cast(List[Frame], self.codec.decode(packet))
        except av.AVError as exc:
            logger.warning("H265Decoder() failed to decode, skipping packet: %s", exc)
            return []


class H265Encoder(Encoder):
    def __init__(self) -> None:
        self.codec: Optional[VideoCodecContext] = None
        self.__target_bitrate = DEFAULT_BITRATE

    def _encode_frame(self, frame: av.VideoFrame, force_keyframe: bool) -> Iterator[bytes]:
        config = _ACTIVE_RUNTIME_CONFIG
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(self.target_bitrate - self.codec.bit_rate) / max(self.codec.bit_rate, 1) > 0.1
        ):
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            self.codec = _open_h265_encoder_context(
                config,
                width=frame.width,
                height=frame.height,
                bitrate=self.target_bitrate,
            )

        data_to_send = b""
        for packet in self.codec.encode(frame):
            data_to_send += bytes(packet)

        if data_to_send:
            yield from _split_annexb_bitstream(data_to_send)

    def encode(self, frame: Frame, force_keyframe: bool = False) -> Tuple[List[bytes], int]:
        assert isinstance(frame, av.VideoFrame)
        payloads = self._packetize(self._encode_frame(frame, force_keyframe))
        timestamp = convert_timebase(frame.pts, frame.time_base, VIDEO_TIME_BASE)
        return payloads, timestamp

    def pack(self, packet: Packet) -> Tuple[List[bytes], int]:
        assert isinstance(packet, av.Packet)
        payloads = self._packetize(_split_annexb_bitstream(bytes(packet)))
        timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
        return payloads, timestamp

    @property
    def target_bitrate(self) -> int:
        return self.__target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate: int) -> None:
        self.__target_bitrate = max(MIN_BITRATE, min(bitrate, MAX_BITRATE))

    @staticmethod
    def _packetize(packages: Iterator[bytes]) -> List[bytes]:
        payloads: List[bytes] = []
        for package in packages:
            if len(package) > PACKET_MAX:
                payloads.extend(_packetize_fu(package))
            else:
                payloads.append(package)
        return payloads


def configure_h265_runtime(config: H265RuntimeConfig, on_message: TextCallback | None = None) -> None:
    global _ACTIVE_RUNTIME_CONFIG, _RUNTIME_MESSAGE_CALLBACK
    _ACTIVE_RUNTIME_CONFIG = config
    _RUNTIME_MESSAGE_CALLBACK = on_message


def h265_depayload(payload: bytes) -> bytes:
    _, data = H265PayloadDescriptor.parse(payload)
    return data


def _emit_once(message: str) -> None:
    if message in _EMITTED_MESSAGES:
        return
    _EMITTED_MESSAGES.add(message)
    if _RUNTIME_MESSAGE_CALLBACK is not None:
        _RUNTIME_MESSAGE_CALLBACK(message)


def _open_h265_encoder_context(
    config: H265RuntimeConfig,
    *,
    width: int,
    height: int,
    bitrate: int,
) -> VideoCodecContext:
    candidates = [(config.codec, config.options)]
    if config.codec != config.fallback_codec:
        candidates.append((config.fallback_codec, config.fallback_options))

    last_error: Exception | None = None
    for codec_name, options in candidates:
        try:
            codec = cast(VideoCodecContext, av.CodecContext.create(codec_name, "w"))
            codec.width = width
            codec.height = height
            codec.bit_rate = max(250_000, min(bitrate, config.max_bitrate_bps))
            codec.pix_fmt = "yuv420p"
            codec.framerate = fractions.Fraction(config.fps, 1)
            codec.time_base = fractions.Fraction(1, config.fps)
            codec.gop_size = config.gop_size
            codec.options = dict(options)
            codec.open()
            if codec_name != config.codec:
                _emit_once(f"{config.codec} could not be opened in this runtime. Falling back to {config.fallback_codec}.")
            return codec
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Unable to open an H.265 encoder context: {last_error}")


def _packetize_fu(data: bytes) -> List[bytes]:
    if len(data) <= HEVC_HEADER_SIZE:
        return [data]

    available_size = PACKET_MAX - HEVC_FU_HEADER_SIZE
    payload_size = len(data) - HEVC_HEADER_SIZE
    packet_count = max(1, math.ceil(payload_size / available_size))
    large_packet_count = payload_size % packet_count
    base_packet_size = payload_size // packet_count

    original_nal_type = (data[0] >> 1) & 0x3F
    payload_header = bytes([(data[0] & 0x81) | (HEVC_NAL_TYPE_FU << 1), data[1]])
    start_header = bytes([0x80 | original_nal_type])
    middle_header = bytes([original_nal_type])
    end_header = bytes([0x40 | original_nal_type])

    packets: List[bytes] = []
    offset = HEVC_HEADER_SIZE
    fu_header = start_header
    while offset < len(data):
        if large_packet_count > 0:
            large_packet_count -= 1
            payload = data[offset : offset + base_packet_size + 1]
            offset += base_packet_size + 1
        else:
            payload = data[offset : offset + base_packet_size]
            offset += base_packet_size

        if offset >= len(data):
            fu_header = end_header

        packets.append(payload_header + fu_header + payload)
        fu_header = middle_header

    return packets


def _split_annexb_bitstream(buf: bytes) -> Iterator[bytes]:
    index = 0
    while True:
        index = buf.find(b"\x00\x00\x01", index)
        if index == -1:
            return
        index += 3
        start = index
        index = buf.find(b"\x00\x00\x01", index)
        if index == -1:
            yield buf[start:]
            return
        if buf[index - 1] == 0:
            yield buf[start : index - 1]
        else:
            yield buf[start:index]
