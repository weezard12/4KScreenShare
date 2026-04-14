from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any


OfferHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SignalingError(RuntimeError):
    """Raised when the SDP signaling exchange fails."""


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    raw = await reader.readline()
    if not raw:
        raise SignalingError("Remote signaling endpoint closed the connection.")
    return json.loads(raw.decode("utf-8"))


async def write_message(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload).encode("utf-8") + b"\n")
    await writer.drain()


class HostSignalingServer:
    def __init__(self, host: str, port: int, pin: str, offer_handler: OfferHandler) -> None:
        self.host = host
        self.port = port
        self.pin = pin
        self.offer_handler = offer_handler
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            payload = await read_message(reader)
            if payload.get("type") != "offer":
                raise SignalingError("Expected a WebRTC offer payload.")
            if payload.get("pin") != self.pin:
                raise SignalingError("The provided session PIN is invalid.")

            answer = await self.offer_handler(payload)
            await write_message(writer, {"status": "ok", "answer": answer})
        except Exception as exc:
            await write_message(writer, {"status": "error", "message": str(exc)})
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


class JoinSignalingClient:
    def __init__(self, host: str, port: int, pin: str) -> None:
        self.host = host
        self.port = port
        self.pin = pin

    async def exchange_offer(self, offer: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=timeout,
        )
        try:
            await write_message(
                writer,
                {
                    "type": "offer",
                    "pin": self.pin,
                    "offer": offer,
                },
            )
            response = await asyncio.wait_for(read_message(reader), timeout=timeout)
            if response.get("status") != "ok":
                raise SignalingError(response.get("message", "Unknown signaling failure."))
            return response["answer"]
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
