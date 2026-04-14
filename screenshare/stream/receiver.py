from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Callable

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)

from screenshare.capture.audio import AudioPlaybackBuffer
from screenshare.network.session import DEFAULT_SIGNALING_PORT, ReconnectPolicy, STUN_SERVER_URLS, quality_from_latency
from screenshare.network.signaling import JoinSignalingClient


FrameCallback = Callable[[object], None]
StatsCallback = Callable[[dict[str, Any]], None]
TextCallback = Callable[[str], None]


def _stat_value(stat: object, field_name: str) -> Any:
    if hasattr(stat, field_name):
        return getattr(stat, field_name)
    if isinstance(stat, dict):
        return stat.get(field_name)
    return None


async def wait_for_ice_complete(pc: RTCPeerConnection, timeout: float = 6.0) -> None:
    if pc.iceGatheringState == "complete":
        return

    event = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_state_change() -> None:
        if pc.iceGatheringState == "complete":
            event.set()

    await asyncio.wait_for(event.wait(), timeout=timeout)


class ViewerClient:
    def __init__(
        self,
        *,
        on_frame: FrameCallback | None = None,
        on_stats: StatsCallback | None = None,
        on_status: TextCallback | None = None,
        on_error: TextCallback | None = None,
    ) -> None:
        self.on_frame = on_frame
        self.on_stats = on_stats
        self.on_status = on_status
        self.on_error = on_error

        self.host = ""
        self.pin = ""
        self.port = DEFAULT_SIGNALING_PORT

        self._pc: RTCPeerConnection | None = None
        self._stats_task: asyncio.Task[None] | None = None
        self._video_task: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_policy = ReconnectPolicy()
        self._playback = AudioPlaybackBuffer()
        self._playback_started = False
        self._volume = 1.0
        self._manual_stop = False
        self._connect_lock = asyncio.Lock()

    async def connect(self, host: str, pin: str, port: int = DEFAULT_SIGNALING_PORT) -> None:
        self.host = host
        self.pin = pin.strip()
        self.port = port
        self._manual_stop = False
        self._reconnect_policy.reset()
        await self._connect_once(initial=True)

    async def disconnect(self) -> None:
        self._manual_stop = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        await self._teardown(close_playback=True)
        self._emit_status("Disconnected.")

    def set_volume(self, volume: float) -> None:
        self._volume = volume
        self._playback.set_volume(volume)

    async def _connect_once(self, *, initial: bool) -> None:
        async with self._connect_lock:
            await self._teardown(close_playback=False)

            pc = RTCPeerConnection(
                RTCConfiguration(iceServers=[RTCIceServer(urls=STUN_SERVER_URLS)])
            )
            self._pc = pc
            video_transceiver = pc.addTransceiver("video", direction="recvonly")
            pc.addTransceiver("audio", direction="recvonly")

            @pc.on("connectionstatechange")
            def _on_connection_state() -> None:
                state = pc.connectionState
                self._emit_status(f"WebRTC state: {state}")
                if state == "connected":
                    self._reconnect_policy.reset()
                elif state in {"failed", "disconnected"} and not self._manual_stop:
                    if self._reconnect_task is None or self._reconnect_task.done():
                        self._reconnect_task = asyncio.create_task(self._attempt_reconnect())

            @pc.on("track")
            def _on_track(track: object) -> None:
                kind = getattr(track, "kind", "")
                if kind == "video":
                    self._video_task = asyncio.create_task(self._consume_video(track))
                elif kind == "audio":
                    if not self._playback_started:
                        try:
                            self._playback.start()
                            self._playback_started = True
                        except Exception as exc:
                            self._emit_error(f"Audio playback could not start: {exc}")
                            return
                    self._playback.set_volume(self._volume)
                    self._audio_task = asyncio.create_task(self._consume_audio(track))

            self._prefer_h264(video_transceiver)
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await wait_for_ice_complete(pc)

            client = JoinSignalingClient(self.host, self.port, self.pin)
            try:
                answer = await client.exchange_offer(
                    {
                        "sdp": pc.localDescription.sdp,
                        "type": pc.localDescription.type,
                    }
                )
            except Exception:
                await self._teardown(close_playback=False)
                raise

            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )
            self._stats_task = asyncio.create_task(self._stats_loop(pc))
            self._emit_status("Connected to host." if initial else "Reconnected to host.")

    async def _attempt_reconnect(self) -> None:
        while not self._manual_stop:
            delay = self._reconnect_policy.next_delay()
            if delay is None:
                self._emit_error("The connection could not be restored after 5 attempts.")
                return
            self._emit_status(f"Connection lost. Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            try:
                await self._connect_once(initial=False)
                return
            except Exception as exc:
                self._emit_status(f"Reconnect failed: {exc}")

    async def _consume_video(self, track: object) -> None:
        try:
            while True:
                frame = await track.recv()
                if self.on_frame is not None:
                    self.on_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._manual_stop:
                self._emit_error(f"Video receive stopped: {exc}")

    async def _consume_audio(self, track: object) -> None:
        try:
            while True:
                frame = await track.recv()
                self._playback.push_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._manual_stop:
                self._emit_error(f"Audio receive stopped: {exc}")

    async def _stats_loop(self, pc: RTCPeerConnection) -> None:
        previous_bytes: dict[str, int] = {}
        while self._pc is pc and pc.connectionState != "closed":
            await asyncio.sleep(1.0)
            bitrate = 0.0
            latency_ms = 0.0
            transport_bytes = None
            jitter_ms = 0.0

            try:
                report = await pc.getStats()
            except Exception:
                continue

            for stat in report.values():
                stat_type = _stat_value(stat, "type")
                if stat_type == "inbound-rtp" and _stat_value(stat, "kind") == "video":
                    key = str(_stat_value(stat, "ssrc"))
                    bytes_received = _stat_value(stat, "bytesReceived")
                    if bytes_received is not None:
                        bytes_value = int(bytes_received)
                        previous = previous_bytes.get(key)
                        if previous is not None:
                            bitrate += (bytes_value - previous) * 8
                        previous_bytes[key] = bytes_value
                    jitter = _stat_value(stat, "jitter")
                    if jitter is not None:
                        jitter_ms = max(jitter_ms, float(jitter) / 90.0)
                if stat_type == "transport":
                    transport_bytes = int(_stat_value(stat, "bytesReceived") or 0)
                if stat_type in {"remote-inbound-rtp", "candidate-pair"}:
                    round_trip = _stat_value(stat, "roundTripTime")
                    if round_trip is not None:
                        latency_ms = float(round_trip) * 1000.0

            if bitrate <= 0.0 and transport_bytes is not None:
                previous = previous_bytes.get("transport")
                if previous is not None:
                    bitrate = max(0.0, (transport_bytes - previous) * 8)
                previous_bytes["transport"] = transport_bytes

            if latency_ms <= 0.0:
                latency_ms = jitter_ms

            quality_color, quality_text = quality_from_latency(latency_ms or 9999)
            if self.on_stats is not None:
                self.on_stats(
                    {
                        "bitrate_bps": bitrate,
                        "latency_ms": latency_ms,
                        "quality_color": quality_color,
                        "quality_text": quality_text,
                    }
                )

    async def _teardown(self, *, close_playback: bool) -> None:
        tasks = [self._stats_task, self._video_task, self._audio_task]
        self._stats_task = None
        self._video_task = None
        self._audio_task = None

        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task

        if self._pc is not None:
            with suppress(Exception):
                await self._pc.close()
            self._pc = None

        if close_playback and self._playback_started:
            self._playback.stop()
            self._playback_started = False

    def _emit_status(self, message: str) -> None:
        if self.on_status is not None:
            self.on_status(message)

    def _emit_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message)

    def _prefer_h264(self, transceiver: object) -> None:
        try:
            capabilities = RTCRtpSender.getCapabilities("video")
            preferred = [
                codec
                for codec in capabilities.codecs
                if codec.mimeType.lower() == "video/h264"
            ]
            if not preferred:
                return
            transceiver.setCodecPreferences(preferred)
        except Exception:
            pass
