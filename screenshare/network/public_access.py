from __future__ import annotations

import base64
import os
import socket
import struct
import zlib
from dataclasses import dataclass


STUN_MAGIC_COOKIE = 0x2112A442
STUN_BINDING_REQUEST = 0x0001
STUN_XOR_MAPPED_ADDRESS = 0x0020
STUN_MAPPED_ADDRESS = 0x0001
JOIN_CODE_VERSION = 1
DEFAULT_STUN_HOST = "stun.l.google.com"
DEFAULT_STUN_PORT = 19302


@dataclass(slots=True)
class JoinTarget:
    host: str
    port: int
    pin: str


@dataclass(slots=True)
class PublicJoinInfo:
    public_host: str | None
    signaling_port: int
    pin: str
    join_code: str | None
    summary: str
    detail: str


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
    payload.append(JOIN_CODE_VERSION)
    payload.append(family)
    payload.extend(struct.pack("!H", int(port)))
    payload.extend(packed_host)
    payload.extend(pin_value.to_bytes(3, byteorder="big"))
    checksum = zlib.crc32(bytes(payload)) & 0xFFFF
    payload.extend(struct.pack("!H", checksum))

    encoded = base64.b32encode(bytes(payload)).decode("ascii").rstrip("=")
    return "-".join(encoded[index:index + 4] for index in range(0, len(encoded), 4))


def decode_join_code(code: str) -> JoinTarget:
    normalized = "".join(character for character in code.upper() if character.isalnum())
    if not normalized:
        raise JoinCodeError("The join code is empty.")
    padding = "=" * ((8 - (len(normalized) % 8)) % 8)
    try:
        raw = base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise JoinCodeError("The join code is invalid or corrupted.") from exc

    if len(raw) < 10:
        raise JoinCodeError("The join code is too short.")

    payload = raw[:-2]
    expected_checksum = struct.unpack("!H", raw[-2:])[0]
    actual_checksum = zlib.crc32(payload) & 0xFFFF
    if expected_checksum != actual_checksum:
        raise JoinCodeError("The join code checksum is invalid.")

    version = payload[0]
    family = payload[1]
    if version != JOIN_CODE_VERSION:
        raise JoinCodeError("The join code version is not supported by this app.")

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

    return JoinTarget(host=host, port=port, pin=pin)


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


def resolve_public_join_info(*, pin: str, signaling_port: int, turn_enabled: bool) -> PublicJoinInfo:
    public_host = detect_public_ip_via_stun()
    if public_host is None:
        return PublicJoinInfo(
            public_host=None,
            signaling_port=signaling_port,
            pin=pin,
            join_code=None,
            summary="Internet join code unavailable",
            detail=(
                "The app could not discover the host's public address through STUN. "
                "LAN sharing still works. For internet sessions, ensure the signaling port is reachable "
                "and that the network allows STUN/TURN traffic."
            ),
        )

    relay_note = (
        "TURN relay is configured for restrictive NATs."
        if turn_enabled
        else "TURN relay is not configured, so some restrictive NATs may still block media."
    )
    join_code = encode_join_code(public_host, signaling_port, pin)
    return PublicJoinInfo(
        public_host=public_host,
        signaling_port=signaling_port,
        pin=pin,
        join_code=join_code,
        summary=f"Internet join code ready for {public_host}:{signaling_port}",
        detail=(
            "The join code resolves to the host signaling endpoint. "
            "WebRTC will still negotiate the actual media route over ICE. "
            f"{relay_note} Forward TCP {signaling_port} on the router if internet viewers cannot reach the host."
        ),
    )


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
