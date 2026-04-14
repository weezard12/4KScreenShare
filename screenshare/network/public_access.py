from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.parse
import zlib
from dataclasses import dataclass

from screenshare.network.upnp import PortMappingLease, try_add_tcp_port_mapping


STUN_MAGIC_COOKIE = 0x2112A442
STUN_BINDING_REQUEST = 0x0001
STUN_XOR_MAPPED_ADDRESS = 0x0020
STUN_MAPPED_ADDRESS = 0x0001
JOIN_CODE_VERSION_DIRECT = 1
JOIN_CODE_VERSION_STRUCTURED = 2
DEFAULT_STUN_HOST = "stun.l.google.com"
DEFAULT_STUN_PORT = 19302


@dataclass(slots=True)
class JoinTarget:
    pin: str
    mode: str = "direct"
    host: str | None = None
    port: int | None = None
    relay_url: str | None = None
    session_id: str | None = None

    @property
    def is_relay(self) -> bool:
        return self.mode == "relay" and bool(self.relay_url) and bool(self.session_id)

    @property
    def display_target(self) -> str:
        if self.is_relay:
            parsed = urllib.parse.urlparse(self.relay_url or "")
            return parsed.netloc or (self.relay_url or "public relay")
        host = self.host or "unknown host"
        return f"{host}:{self.port or 0}"


@dataclass(slots=True)
class PublicJoinInfo:
    ready: bool
    mode: str
    join_code: str | None
    summary: str
    detail: str
    endpoint_text: str


class JoinCodeError(ValueError):
    """Raised when the provided join code cannot be decoded."""


def encode_join_code(host: str, port: int, pin: str) -> str:
    normalized_pin = pin.strip()
    if len(normalized_pin) != 6 or not normalized_pin.isdigit():
        raise JoinCodeError("Join codes require a 6-digit session PIN.")

    try:
        packed_host = socket.inet_pton(socket.AF_INET, host)
        family = 0x04
    except OSError:
        try:
            packed_host = socket.inet_pton(socket.AF_INET6, host)
            family = 0x06
        except OSError as exc:
            raise JoinCodeError("Join codes only support valid IPv4 or IPv6 addresses.") from exc

    pin_value = int(normalized_pin)
    payload = bytearray()
    payload.append(JOIN_CODE_VERSION_DIRECT)
    payload.append(family)
    payload.extend(struct.pack("!H", int(port)))
    payload.extend(packed_host)
    payload.extend(pin_value.to_bytes(3, byteorder="big"))
    checksum = zlib.crc32(bytes(payload)) & 0xFFFF
    payload.extend(struct.pack("!H", checksum))

    return _base32_join_code(bytes(payload))


def encode_relay_join_code(*, relay_url: str, session_id: str, pin: str) -> str:
    normalized_pin = pin.strip()
    if len(normalized_pin) != 6 or not normalized_pin.isdigit():
        raise JoinCodeError("Join codes require a 6-digit session PIN.")

    payload = {
        "m": "relay",
        "u": relay_url.rstrip("/"),
        "s": session_id.strip().upper(),
        "p": normalized_pin,
    }
    encoded_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    compressed = zlib.compress(encoded_json, level=9)
    body = bytes([JOIN_CODE_VERSION_STRUCTURED]) + compressed
    checksum = zlib.crc32(body) & 0xFFFF
    return _base32_join_code(body + struct.pack("!H", checksum))


def decode_join_code(code: str) -> JoinTarget:
    raw = _decode_base32_join_code(code)
    if len(raw) < 3:
        raise JoinCodeError("The join code is too short.")

    version = raw[0]
    if version == JOIN_CODE_VERSION_DIRECT:
        return _decode_direct_join_code(raw)
    if version == JOIN_CODE_VERSION_STRUCTURED:
        return _decode_structured_join_code(raw)
    raise JoinCodeError("The join code version is not supported by this app.")


def detect_public_ip_via_stun(
    *,
    stun_host: str = DEFAULT_STUN_HOST,
    stun_port: int = DEFAULT_STUN_PORT,
    timeout: float = 2.5,
) -> str | None:
    transaction_id = os.urandom(12)
    request = struct.pack("!HHI12s", STUN_BINDING_REQUEST, 0, STUN_MAGIC_COOKIE, transaction_id)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, (stun_host, stun_port))
        response, _ = sock.recvfrom(2048)
    except OSError:
        return None
    finally:
        sock.close()

    return _parse_stun_response(response, transaction_id)


def resolve_relay_join_info(
    *,
    relay_url: str,
    session_id: str,
    pin: str,
    relay_connected: bool,
) -> PublicJoinInfo:
    join_code = encode_relay_join_code(relay_url=relay_url, session_id=session_id, pin=pin)
    relay_host = urllib.parse.urlparse(relay_url).netloc or relay_url
    status = "ready" if relay_connected else "connecting"
    detail = (
        "The host is connected to the public signaling relay."
        if relay_connected
        else "The host is still connecting to the public signaling relay. Internet viewers can join once the relay connection is established."
    )
    return PublicJoinInfo(
        ready=relay_connected,
        mode="relay",
        join_code=join_code,
        summary=f"Internet join code {status} via relay",
        detail=detail,
        endpoint_text=f"Relay endpoint  {relay_host}",
    )


def resolve_direct_join_info(
    *,
    internal_host_ip: str,
    signaling_port: int,
    pin: str,
    turn_enabled: bool,
) -> tuple[PublicJoinInfo, PortMappingLease | None]:
    public_host = detect_public_ip_via_stun()
    if public_host is None:
        return (
            PublicJoinInfo(
                ready=False,
                mode="direct",
                join_code=None,
                summary="Internet join unavailable",
                detail=(
                    "The app could not discover the host's public address through STUN. "
                    "LAN sharing still works. For internet sessions, configure a public signaling relay or ensure the network allows STUN."
                ),
                endpoint_text="Public endpoint unavailable",
            ),
            None,
        )

    lease = try_add_tcp_port_mapping(
        internal_client=internal_host_ip,
        internal_port=signaling_port,
        preferred_external_port=signaling_port,
        description="4KScreenShare signaling",
    )
    if lease is None:
        relay_note = (
            "TURN is configured for media, but signaling still needs either a public relay or a reachable router mapping."
            if turn_enabled
            else "Configure TURN for media and use either a public relay or a reachable router mapping for signaling."
        )
        return (
            PublicJoinInfo(
                ready=False,
                mode="direct",
                join_code=None,
                summary="Internet join needs router or relay setup",
                detail=(
                    "The host's router did not accept an automatic UPnP port mapping for signaling. "
                    f"{relay_note} Forward TCP {signaling_port} manually or configure SCREENSHARE_SIGNALING_RELAY_URL."
                ),
                endpoint_text=f"Public IP detected  {public_host}",
            ),
            None,
        )

    external_host = lease.external_ip or public_host
    join_code = encode_join_code(external_host, lease.external_port, pin)
    relay_note = (
        "TURN is configured for restrictive NATs."
        if turn_enabled
        else "TURN is not configured, so some restrictive NATs may still require a relay."
    )
    return (
        PublicJoinInfo(
            ready=True,
            mode="direct",
            join_code=join_code,
            summary="Internet join code ready through router mapping",
            detail=f"Automatic UPnP router mapping is active for TCP {lease.external_port}. {relay_note}",
            endpoint_text=f"Public endpoint  {external_host}:{lease.external_port}",
        ),
        lease,
    )


def _base32_join_code(raw: bytes) -> str:
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(encoded[index:index + 4] for index in range(0, len(encoded), 4))


def _decode_base32_join_code(code: str) -> bytes:
    normalized = "".join(character for character in code.upper() if character.isalnum())
    if not normalized:
        raise JoinCodeError("The join code is empty.")
    padding = "=" * ((8 - (len(normalized) % 8)) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise JoinCodeError("The join code is invalid or corrupted.") from exc


def _decode_direct_join_code(raw: bytes) -> JoinTarget:
    if len(raw) < 10:
        raise JoinCodeError("The join code is too short.")

    payload = raw[:-2]
    expected_checksum = struct.unpack("!H", raw[-2:])[0]
    actual_checksum = zlib.crc32(payload) & 0xFFFF
    if expected_checksum != actual_checksum:
        raise JoinCodeError("The join code checksum is invalid.")

    family = payload[1]
    try:
        port = struct.unpack("!H", payload[2:4])[0]
    except struct.error as exc:
        raise JoinCodeError("The join code does not contain a valid signaling port.") from exc

    if family == 0x04:
        host_end = 8
        if len(payload) != 11:
            raise JoinCodeError("The join code payload is malformed.")
        host = socket.inet_ntop(socket.AF_INET, payload[4:host_end])
    elif family == 0x06:
        host_end = 20
        if len(payload) != 23:
            raise JoinCodeError("The join code payload is malformed.")
        host = socket.inet_ntop(socket.AF_INET6, payload[4:host_end])
    else:
        raise JoinCodeError("The join code address family is not supported.")

    pin = f"{int.from_bytes(payload[host_end:host_end + 3], byteorder='big'):06d}"
    if not host or port <= 0 or port > 65535:
        raise JoinCodeError("The join code does not contain a complete session target.")
    return JoinTarget(mode="direct", host=host, port=port, pin=pin)


def _decode_structured_join_code(raw: bytes) -> JoinTarget:
    payload = raw[:-2]
    expected_checksum = struct.unpack("!H", raw[-2:])[0]
    actual_checksum = zlib.crc32(payload) & 0xFFFF
    if expected_checksum != actual_checksum:
        raise JoinCodeError("The join code checksum is invalid.")

    try:
        payload_data = json.loads(zlib.decompress(payload[1:]).decode("utf-8"))
    except Exception as exc:
        raise JoinCodeError("The join code payload is malformed.") from exc

    mode = str(payload_data.get("m", "")).strip().lower()
    pin = str(payload_data.get("p", "")).strip()
    if len(pin) != 6 or not pin.isdigit():
        raise JoinCodeError("The join code is missing a valid session PIN.")

    if mode == "relay":
        relay_url = str(payload_data.get("u", "")).strip().rstrip("/")
        session_id = str(payload_data.get("s", "")).strip().upper()
        if not relay_url or not session_id:
            raise JoinCodeError("The relay join code is incomplete.")
        return JoinTarget(
            mode="relay",
            relay_url=relay_url,
            session_id=session_id,
            pin=pin,
        )
    raise JoinCodeError("The join code mode is not supported by this app.")


def _parse_stun_response(response: bytes, transaction_id: bytes) -> str | None:
    if len(response) < 20:
        return None

    _message_type, message_length, cookie = struct.unpack("!HHI", response[:8])
    received_transaction_id = response[8:20]
    if cookie != STUN_MAGIC_COOKIE or received_transaction_id != transaction_id:
        return None

    limit = min(len(response), 20 + message_length)
    offset = 20
    while offset + 4 <= limit:
        attribute_type, attribute_length = struct.unpack("!HH", response[offset:offset + 4])
        value_start = offset + 4
        value_end = value_start + attribute_length
        if value_end > limit:
            return None
        value = response[value_start:value_end]

        if attribute_type == STUN_XOR_MAPPED_ADDRESS:
            address = _decode_xor_mapped_address(value, transaction_id)
            if address is not None:
                return address
        elif attribute_type == STUN_MAPPED_ADDRESS:
            address = _decode_mapped_address(value)
            if address is not None:
                return address

        offset = value_end + ((4 - (attribute_length % 4)) % 4)
    return None


def _decode_xor_mapped_address(value: bytes, transaction_id: bytes) -> str | None:
    if len(value) < 8:
        return None
    family = value[1]
    if family == 0x01 and len(value) >= 8:
        ip_bytes = bytes(
            source ^ mask
            for source, mask in zip(
                value[4:8],
                struct.pack("!I", STUN_MAGIC_COOKIE),
            )
        )
        return socket.inet_ntoa(ip_bytes)
    if family == 0x02 and len(value) >= 20:
        mask = struct.pack("!I", STUN_MAGIC_COOKIE) + transaction_id
        ip_bytes = bytes(source ^ salt for source, salt in zip(value[4:20], mask))
        return socket.inet_ntop(socket.AF_INET6, ip_bytes)
    return None


def _decode_mapped_address(value: bytes) -> str | None:
    if len(value) < 8:
        return None
    family = value[1]
    if family == 0x01 and len(value) >= 8:
        return socket.inet_ntoa(value[4:8])
    if family == 0x02 and len(value) >= 20:
        return socket.inet_ntop(socket.AF_INET6, value[4:20])
    return None
