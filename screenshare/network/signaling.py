from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web


OfferHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
StatusCallback = Callable[[str], None]


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
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise SignalingError(
                f"The viewer could not reach {self.host}:{self.port}. "
                "For internet sessions, the host needs a public relay or a reachable router mapping."
            ) from exc
        except OSError as exc:
            reason = exc.strerror or str(exc) or "Unknown socket error."
            raise SignalingError(f"Could not connect to {self.host}:{self.port}: {reason}") from exc

        try:
            await write_message(
                writer,
                {
                    "type": "offer",
                    "pin": self.pin,
                    "offer": offer,
                },
            )
            try:
                response = await asyncio.wait_for(read_message(reader), timeout=timeout)
            except TimeoutError as exc:
                raise SignalingError(
                    f"The host did not answer on {self.host}:{self.port} in time."
                ) from exc
            if response.get("status") != "ok":
                raise SignalingError(response.get("message", "Unknown signaling failure."))
            return response["answer"]
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


class RelayHostSignalingClient:
    def __init__(
        self,
        relay_url: str,
        session_id: str,
        pin: str,
        offer_handler: OfferHandler,
        *,
        on_status: StatusCallback | None = None,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.session_id = session_id.strip().upper()
        self.pin = pin
        self.offer_handler = offer_handler
        self.on_status = on_status

        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._connected = False
        self._initial_attempt_done = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start(self, *, initial_timeout: float = 5.0) -> bool:
        if self._task is not None and not self._task.done():
            return self._connected
        self._closing = False
        self._initial_attempt_done = asyncio.Event()
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._initial_attempt_done.wait(), timeout=initial_timeout)
        except TimeoutError:
            pass
        return self._connected

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._connected = False

    async def _run(self) -> None:
        backoff = 1.0
        try:
            while not self._closing:
                try:
                    await self._run_once()
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._connected = False
                    self._emit_status(f"Public relay unavailable: {exc}")
                finally:
                    if not self._initial_attempt_done.is_set():
                        self._initial_attempt_done.set()
                if self._closing:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)
        finally:
            self._connected = False

    async def _run_once(self) -> None:
        if self._session is None:
            raise SignalingError("Relay session is not initialized.")
        ws_url = _join_url(self.relay_url, "/ws/host")
        async with self._session.ws_connect(
            ws_url,
            heartbeat=20.0,
            params={"session_id": self.session_id, "pin": self.pin},
        ) as websocket:
            self._connected = True
            self._emit_status("Public signaling relay connected.")
            if not self._initial_attempt_done.is_set():
                self._initial_attempt_done.set()

            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(message.data)
                    await self._handle_relay_message(websocket, payload)
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise SignalingError(f"Relay WebSocket error: {websocket.exception()}")

        self._connected = False
        if not self._closing:
            raise SignalingError("Relay connection closed.")

    async def _handle_relay_message(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any],
    ) -> None:
        if payload.get("type") != "offer":
            return
        request_id = str(payload.get("request_id", "")).strip()
        try:
            answer = await self.offer_handler(
                {
                    "type": "offer",
                    "pin": payload.get("pin", ""),
                    "offer": payload.get("offer", {}),
                }
            )
            response = {"type": "answer", "request_id": request_id, "status": "ok", "answer": answer}
        except Exception as exc:
            response = {"type": "answer", "request_id": request_id, "status": "error", "message": str(exc)}
        await websocket.send_json(response)

    def _emit_status(self, message: str) -> None:
        if self.on_status is not None:
            self.on_status(message)


class RelayJoinSignalingClient:
    def __init__(self, relay_url: str, session_id: str, pin: str) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.session_id = session_id.strip().upper()
        self.pin = pin

    async def exchange_offer(self, offer: dict[str, Any], timeout: float = 25.0) -> dict[str, Any]:
        request_url = _join_url(self.relay_url, f"/api/sessions/{self.session_id}/connect")
        timeout_config = aiohttp.ClientTimeout(total=timeout)
        payload = {"pin": self.pin, "offer": offer}
        try:
            async with aiohttp.ClientSession(timeout=timeout_config) as session:
                async with session.post(request_url, json=payload) as response:
                    body = await response.json()
        except TimeoutError as exc:
            raise SignalingError("The public signaling relay did not answer in time.") from exc
        except aiohttp.ClientError as exc:
            raise SignalingError(f"Could not reach the public signaling relay: {exc}") from exc

        if body.get("status") != "ok":
            raise SignalingError(body.get("message", "Unknown relay signaling failure."))
        return body["answer"]


class EmbeddedRelayServer:
    def __init__(self) -> None:
        self._app = create_relay_app()
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise SignalingError("The embedded relay has not started yet.")
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> str:
        if self._runner is not None:
            return self.base_url

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await self._site.start()
        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None) or []
        if not sockets:
            await self.stop()
            raise SignalingError("The embedded relay did not expose a listening socket.")
        self._port = int(sockets[0].getsockname()[1])
        return self.base_url

    async def stop(self) -> None:
        if self._site is not None:
            with suppress(Exception):
                await self._site.stop()
            self._site = None
        if self._runner is not None:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        self._port = None


@dataclass(slots=True)
class _RelayHostChannel:
    websocket: web.WebSocketResponse
    pin: str


class SignalingRelayHub:
    def __init__(self) -> None:
        self._hosts: dict[str, _RelayHostChannel] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def register_host(self, session_id: str, websocket: web.WebSocketResponse, pin: str) -> None:
        async with self._lock:
            previous = self._hosts.get(session_id)
            self._hosts[session_id] = _RelayHostChannel(websocket=websocket, pin=pin)
        if previous is not None and previous.websocket is not websocket:
            await previous.websocket.close(code=4000, message=b"replaced")

    async def unregister_host(self, session_id: str, websocket: web.WebSocketResponse) -> None:
        async with self._lock:
            channel = self._hosts.get(session_id)
            if channel is not None and channel.websocket is websocket:
                self._hosts.pop(session_id, None)

    async def dispatch_offer(self, session_id: str, pin: str, offer: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        async with self._lock:
            channel = self._hosts.get(session_id)
            if channel is None:
                raise SignalingError("No host is connected for that internet join code.")
            if channel.pin != pin:
                raise SignalingError("The provided session PIN is invalid.")
            request_id = secrets.token_hex(8)
            loop = asyncio.get_running_loop()
            response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[request_id] = response_future

        try:
            await channel.websocket.send_json(
                {
                    "type": "offer",
                    "request_id": request_id,
                    "pin": pin,
                    "offer": offer,
                }
            )
        except Exception as exc:
            async with self._lock:
                self._pending.pop(request_id, None)
            raise SignalingError(f"The host relay channel is unavailable: {exc}") from exc

        try:
            response = await asyncio.wait_for(response_future, timeout=timeout)
        except TimeoutError as exc:
            raise SignalingError("The host did not answer through the public relay in time.") from exc
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

        if response.get("status") != "ok":
            raise SignalingError(response.get("message", "Unknown relay signaling failure."))
        return response["answer"]

    async def resolve_answer(self, request_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(payload)


def create_relay_app() -> web.Application:
    app = web.Application()
    hub = SignalingRelayHub()
    app["hub"] = hub
    app.add_routes(
        [
            web.get("/health", _relay_health),
            web.get("/ws/host", _relay_host_ws),
            web.post("/api/sessions/{session_id}/connect", _relay_connect),
        ]
    )
    return app


async def _relay_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _relay_host_ws(request: web.Request) -> web.StreamResponse:
    session_id = request.query.get("session_id", "").strip().upper()
    pin = request.query.get("pin", "").strip()
    if not session_id or not pin:
        raise web.HTTPBadRequest(text="session_id and pin are required.")

    websocket = web.WebSocketResponse(heartbeat=20.0)
    await websocket.prepare(request)

    hub: SignalingRelayHub = request.app["hub"]
    await hub.register_host(session_id, websocket, pin)
    await websocket.send_json({"type": "ready", "session_id": session_id})

    try:
        async for message in websocket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            if payload.get("type") == "answer":
                request_id = str(payload.get("request_id", "")).strip()
                await hub.resolve_answer(request_id, payload)
    finally:
        await hub.unregister_host(session_id, websocket)

    return websocket


async def _relay_connect(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"].strip().upper()
    body = await request.json()
    pin = str(body.get("pin", "")).strip()
    offer = body.get("offer")
    if not pin or not isinstance(offer, dict):
        raise web.HTTPBadRequest(text="pin and offer are required.")

    hub: SignalingRelayHub = request.app["hub"]
    try:
        answer = await hub.dispatch_offer(session_id, pin, offer, timeout=25.0)
    except SignalingError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    return web.json_response({"status": "ok", "answer": answer})


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"
