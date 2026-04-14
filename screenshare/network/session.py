from __future__ import annotations

import os
import random
import secrets
import socket
from dataclasses import dataclass, field

from screenshare.stream.video_codecs import VIDEO_CODEC_H264, VIDEO_CODEC_LABELS


DEFAULT_SIGNALING_PORT = 8765
STUN_SERVER_URLS = ["stun:stun.l.google.com:19302"]
TURN_URLS_ENV = "SCREENSHARE_TURN_URLS"
TURN_USERNAME_ENV = "SCREENSHARE_TURN_USERNAME"
TURN_CREDENTIAL_ENV = "SCREENSHARE_TURN_CREDENTIAL"
SIGNALING_RELAY_URL_ENV = "SCREENSHARE_SIGNALING_RELAY_URL"

QUALITY_PRESETS = ("High", "Balanced", "Low-latency")
FPS_PRESETS = (60, 30, 15)
VIDEO_CODEC_PRESETS = VIDEO_CODEC_LABELS


@dataclass(slots=True)
class HostSessionConfig:
    pin: str
    host_ip: str
    relay_session_id: str
    monitor_index: int
    monitor_label: str
    monitor_region: dict[str, int]
    resolution_label: str
    frame_width: int
    frame_height: int
    fps: int
    quality: str
    share_system_audio: bool
    share_microphone: bool
    video_codec: str = VIDEO_CODEC_H264
    signaling_port: int = DEFAULT_SIGNALING_PORT


@dataclass(slots=True)
class ReconnectPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    cap_delay: float = 12.0
    _attempt: int = field(default=0, init=False)

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float | None:
        if self._attempt >= self.max_attempts:
            return None
        delay = min(self.base_delay * (2 ** self._attempt), self.cap_delay)
        self._attempt += 1
        return delay


def generate_session_pin() -> str:
    return f"{random.randint(0, 999999):06d}"


def generate_public_session_id(length: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def detect_local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def configured_signaling_relay_url() -> str | None:
    value = os.getenv(SIGNALING_RELAY_URL_ENV, "").strip()
    if not value:
        return None
    return value.rstrip("/")


def ice_server_settings() -> list[dict[str, object]]:
    settings: list[dict[str, object]] = [{"urls": list(STUN_SERVER_URLS)}]
    turn_urls = [value.strip() for value in os.getenv(TURN_URLS_ENV, "").split(",") if value.strip()]
    if not turn_urls:
        return settings

    turn_server: dict[str, object] = {"urls": turn_urls}
    username = os.getenv(TURN_USERNAME_ENV, "").strip()
    credential = os.getenv(TURN_CREDENTIAL_ENV, "").strip()
    if username:
        turn_server["username"] = username
    if credential:
        turn_server["credential"] = credential
    settings.append(turn_server)
    return settings


def has_turn_server_config() -> bool:
    return len(ice_server_settings()) > 1


def format_bitrate(bits_per_second: float) -> str:
    if bits_per_second <= 0:
        return "0 Mbps"
    return f"{bits_per_second / 1_000_000:.2f} Mbps"


def parse_bitrate_to_bps(bitrate_str: str) -> int:
    """Parse '5M', '1500K' to integer bits per second."""
    br = bitrate_str.strip().upper()
    try:
        if br.endswith("M"):
            return int(float(br[:-1]) * 1_000_000)
        if br.endswith("K"):
            return int(float(br[:-1]) * 1_000)
        return int(br)
    except Exception:
        return 1_000_000


def parse_bitrate_to_kbps(bitrate_str: str) -> int:
    return parse_bitrate_to_bps(bitrate_str) // 1000


def quality_from_latency(latency_ms: float) -> tuple[str, str]:
    if latency_ms <= 65:
        return ("#26c281", "Green: low latency and healthy transport stats.")
    if latency_ms <= 140:
        return ("#f2c94c", "Yellow: usable link with moderate delay or jitter.")
    return ("#eb5757", "Red: unstable connection or high delay.")
