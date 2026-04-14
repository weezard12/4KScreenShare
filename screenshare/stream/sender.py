from __future__ import annotations

import asyncio
import io
import secrets
import subprocess
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

import av
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.mediastreams import MediaStreamError
from aiortc.contrib.media import MediaRelay
from av import VideoFrame

from screenshare.capture.audio import CompositeAudioTrack
from screenshare.network.public_access import (
    PublicJoinInfo,
    resolve_direct_join_info,
    resolve_relay_join_info,
)
from screenshare.capture.screen import ScreenCaptureWorker
from screenshare.network.session import (
    HostSessionConfig,
    configured_signaling_relay_url,
    ice_server_settings,
    parse_bitrate_to_kbps,
)
from screenshare.network.quick_tunnel import QuickTunnelProcess
from screenshare.network.signaling import EmbeddedRelayServer, HostSignalingServer, RelayHostSignalingClient
from screenshare.network.upnp import PortMappingLease
from screenshare.stream.encoder import (
    apply_runtime_video_profile,
    build_ffmpeg_desktop_packet_command,
    build_ffmpeg_packet_command,
    detect_best_encoder_for_format,
    ffmpeg_supports_filter,
    resolve_quality_profile,
)
from screenshare.stream.video_codecs import ensure_webrtc_video_codecs_registered, preferred_video_capabilities
from screenshare.utils.process import hidden_subprocess_kwargs
from screenshare.utils.resolution import scale_to_fit


StatsCallback = Callable[[dict[str, Any]], None]
TextCallback = Callable[[str], None]


def _stat_value(stat: object, field_name: str) -> Any:
    if hasattr(stat, field_name):
        return getattr(stat, field_name)
    if isinstance(stat, dict):
        return stat.get(field_name)
    return None


class _NonSeekablePipe(io.RawIOBase):
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        self._position += len(data)
        return data

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 1 and offset == 0:
            return self._position
        if whence == 0 and offset == self._position:
            return self._position
        raise OSError(29, "Illegal seek")


async def wait_for_ice_complete(pc: RTCPeerConnection, timeout: float = 6.0) -> None:
    if pc.iceGatheringState == "complete":
        return

    event = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_state_change() -> None:
        if pc.iceGatheringState == "complete":
            event.set()

    await asyncio.wait_for(event.wait(), timeout=timeout)


class SharedScreenTrack(VideoStreamTrack):
    def __init__(self, capture_worker: ScreenCaptureWorker) -> None:
        super().__init__()
        self.capture_worker = capture_worker
        self._last_sequence = 0

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        loop = asyncio.get_running_loop()
        frame_data = await loop.run_in_executor(
            None,
            lambda: self.capture_worker.get_latest_frame(last_sequence=self._last_sequence),
        )
        self._last_sequence = frame_data.sequence
        frame = VideoFrame.from_ndarray(frame_data.array, format="rgb24")
        frame.pts = pts
        frame.time_base = time_base
        return frame


class FFmpegPacketTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self,
        capture_worker: ScreenCaptureWorker,
        *,
        selection,
        input_width: int,
        input_height: int,
        width: int,
        height: int,
        fps: int,
        quality_profile,
        on_message: TextCallback | None = None,
    ) -> None:
        super().__init__()
        self.capture_worker = capture_worker
        self.selection = selection
        self.input_width = input_width
        self.input_height = input_height
        self.width = width
        self.height = height
        self.fps = fps
        self.quality_profile = quality_profile
        self.on_message = on_message

        # Keep the transport queue intentionally short to favor near-live delivery.
        packet_queue_size = 64 if (width * height) > (1920 * 1080) else max(8, min(16, fps // 2 or 1))
        self._queue: asyncio.Queue[av.Packet | None] = asyncio.Queue(maxsize=packet_queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_started = False
        self._quit = threading.Event()
        self._writer_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()
        self._last_sequence = 0
        self._packet_times: deque[float] = deque(maxlen=max(120, fps * 4))

    async def recv(self) -> av.Packet:
        if self.readyState != "live":
            raise MediaStreamError

        if not self._worker_started:
            self._loop = asyncio.get_running_loop()
            self._start_workers()

        packet = await self._queue.get()
        if packet is None:
            self.stop()
            raise MediaStreamError
        return packet

    def stop(self) -> None:
        if self.readyState == "ended":
            return
        self._quit.set()
        process = self._process
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            with suppress(Exception):
                process.kill()
        current = threading.current_thread()
        for thread in (self._writer_thread, self._reader_thread):
            if thread is not None and thread.is_alive() and thread is not current:
                thread.join(timeout=1.5)
        super().stop()

    def _start_workers(self) -> None:
        self._worker_started = True
        self._writer_thread = threading.Thread(target=self._writer_worker, name="ffmpeg-packet-writer", daemon=True)
        self._reader_thread = threading.Thread(target=self._reader_worker, name="ffmpeg-packet-reader", daemon=True)
        self._writer_thread.start()
        self._reader_thread.start()

    def _writer_worker(self) -> None:
        try:
            process = self._ensure_process()
            while not self._quit.is_set():
                frame = self.capture_worker.get_latest_frame(last_sequence=self._last_sequence, timeout=2.0)
                self._last_sequence = frame.sequence
                if frame.pixel_format == "bgra" and frame.raw_bytes is not None:
                    if frame.width != self.input_width or frame.height != self.input_height:
                        self._emit_message(
                            f"Capture resolution changed to {frame.width}x{frame.height}. Restart the session to apply NVENC at the new size."
                        )
                        self._queue_terminal()
                        return
                    payload = frame.raw_bytes
                else:
                    if frame.array is None:
                        raise RuntimeError("Captured frame data is unavailable.")
                    if frame.width != self.width or frame.height != self.height:
                        self._emit_message(
                            f"Capture resolution changed to {frame.width}x{frame.height}. Restart the session to apply NVENC at the new size."
                        )
                        self._queue_terminal()
                        return
                    payload = memoryview(frame.array).cast("B")
                if process.stdin is None:
                    raise RuntimeError("FFmpeg stdin is unavailable.")
                process.stdin.write(payload)
        except Exception as exc:
            self._emit_message(f"NVENC packet pipeline stopped: {exc}")
            self._queue_terminal()
            self.stop()

    def _reader_worker(self) -> None:
        container = None
        first_pts: int | None = None
        waiting_for_keyframe = True
        try:
            process = self._ensure_process()
            if process.stdout is None:
                raise RuntimeError("FFmpeg stdout is unavailable.")
            container = av.open(
                _NonSeekablePipe(process.stdout),
                format="mpegts",
                mode="r",
                options={
                    "fflags": "nobuffer",
                    "flags": "low_delay",
                    "probesize": "32",
                    "analyzeduration": "0",
                },
            )
            video_stream = next((stream for stream in container.streams if stream.type == "video"), None)
            if video_stream is None:
                raise RuntimeError("FFmpeg did not expose a video stream.")

            for packet in container.demux(video_stream):
                if self._quit.is_set():
                    break
                if not packet.size or packet.pts is None:
                    continue
                if waiting_for_keyframe:
                    if not packet.is_keyframe:
                        continue
                    waiting_for_keyframe = False
                if first_pts is None:
                    first_pts = packet.pts
                packet.pts -= first_pts
                packet.dts = None if packet.dts is None else packet.dts - first_pts
                self._queue_packet(packet)
        except Exception as exc:
            if not self._quit.is_set():
                self._emit_message(f"NVENC packet pipeline read failed: {exc}")
        finally:
            if container is not None:
                with suppress(Exception):
                    container.close()
            self._queue_terminal()

    def _ensure_process(self):
        with self._process_lock:
            if self._process is not None:
                return self._process
            command = build_ffmpeg_packet_command(
                self.selection,
                input_width=self.input_width,
                input_height=self.input_height,
                output_width=self.width,
                output_height=self.height,
                fps=self.fps,
                quality_profile=self.quality_profile,
                input_pixel_format="bgra" if self.capture_worker.prefer_raw_bgra else "rgb24",
                output_target="pipe:1",
            )
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                **hidden_subprocess_kwargs(),
            )
            return self._process

    def _queue_packet(self, packet: av.Packet) -> None:
        if self._loop is None or self._quit.is_set():
            return
        self._packet_times.append(time.perf_counter())
        self._loop.call_soon_threadsafe(self._put_latest_packet, packet)

    def _queue_terminal(self) -> None:
        if self._loop is None or self._quit.is_set():
            return
        self._quit.set()
        self._loop.call_soon_threadsafe(self._put_latest_packet, None)

    def _emit_message(self, message: str) -> None:
        if self.on_message is not None:
            self.on_message(message)

    def _put_latest_packet(self, packet: av.Packet | None) -> None:
        if self._quit.is_set() and packet is not None:
            return
        try:
            self._queue.put_nowait(packet)
            return
        except asyncio.QueueFull:
            pass

        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(packet)

    def get_capture_fps(self) -> float:
        if len(self._packet_times) < 2:
            return float(self.fps)
        elapsed = self._packet_times[-1] - self._packet_times[0]
        if elapsed <= 0:
            return float(self.fps)
        return (len(self._packet_times) - 1) / elapsed


class FFmpegDesktopCaptureTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self,
        *,
        selection,
        monitor_index: int,
        input_width: int,
        input_height: int,
        width: int,
        height: int,
        fps: int,
        quality_profile,
        include_cursor: bool = False,
        on_message: TextCallback | None = None,
    ) -> None:
        super().__init__()
        self.selection = selection
        self.monitor_index = monitor_index
        self.input_width = input_width
        self.input_height = input_height
        self.width = width
        self.height = height
        self.fps = fps
        self.quality_profile = quality_profile
        self.include_cursor = include_cursor
        self.on_message = on_message

        packet_queue_size = 64 if (width * height) > (1920 * 1080) else max(8, min(16, fps // 2 or 1))
        self._queue: asyncio.Queue[av.Packet | None] = asyncio.Queue(maxsize=packet_queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_started = False
        self._quit = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()
        self._packet_times: deque[float] = deque(maxlen=max(120, fps * 4))

    async def recv(self) -> av.Packet:
        if self.readyState != "live":
            raise MediaStreamError

        if not self._worker_started:
            self._loop = asyncio.get_running_loop()
            self._start_worker()

        packet = await self._queue.get()
        if packet is None:
            self.stop()
            raise MediaStreamError
        return packet

    def stop(self) -> None:
        if self.readyState == "ended":
            return
        self._quit.set()
        process = self._process
        if process is not None:
            with suppress(Exception):
                process.kill()
        current = threading.current_thread()
        if self._reader_thread is not None and self._reader_thread.is_alive() and self._reader_thread is not current:
            self._reader_thread.join(timeout=1.5)
        super().stop()

    def _start_worker(self) -> None:
        self._worker_started = True
        self._reader_thread = threading.Thread(
            target=self._reader_worker,
            name="ffmpeg-ddagrab-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _reader_worker(self) -> None:
        container = None
        first_pts: int | None = None
        waiting_for_keyframe = True
        try:
            process = self._ensure_process()
            if process.stdout is None:
                raise RuntimeError("FFmpeg stdout is unavailable.")
            container = av.open(
                _NonSeekablePipe(process.stdout),
                format="mpegts",
                mode="r",
                options={
                    "fflags": "nobuffer",
                    "flags": "low_delay",
                    "probesize": "32",
                    "analyzeduration": "0",
                },
            )
            video_stream = next((stream for stream in container.streams if stream.type == "video"), None)
            if video_stream is None:
                raise RuntimeError("FFmpeg did not expose a video stream.")

            for packet in container.demux(video_stream):
                if self._quit.is_set():
                    break
                if not packet.size or packet.pts is None:
                    continue
                if waiting_for_keyframe:
                    if not packet.is_keyframe:
                        continue
                    waiting_for_keyframe = False
                if first_pts is None:
                    first_pts = packet.pts
                packet.pts -= first_pts
                packet.dts = None if packet.dts is None else packet.dts - first_pts
                self._queue_packet(packet)
        except Exception as exc:
            if not self._quit.is_set():
                self._emit_message(f"Desktop duplication pipeline stopped: {exc}")
        finally:
            if container is not None:
                with suppress(Exception):
                    container.close()
            self._queue_terminal()

    def _ensure_process(self):
        with self._process_lock:
            if self._process is not None:
                return self._process
            command = build_ffmpeg_desktop_packet_command(
                self.selection,
                monitor_index=self.monitor_index,
                input_width=self.input_width,
                input_height=self.input_height,
                output_width=self.width,
                output_height=self.height,
                fps=self.fps,
                quality_profile=self.quality_profile,
                include_cursor=self.include_cursor,
                output_target="pipe:1",
            )
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                **hidden_subprocess_kwargs(),
            )
            return self._process

    def _queue_packet(self, packet: av.Packet) -> None:
        if self._loop is None or self._quit.is_set():
            return
        self._packet_times.append(time.perf_counter())
        self._loop.call_soon_threadsafe(self._put_latest_packet, packet)

    def _queue_terminal(self) -> None:
        if self._loop is None or self._quit.is_set():
            return
        self._quit.set()
        self._loop.call_soon_threadsafe(self._put_latest_packet, None)

    def _emit_message(self, message: str) -> None:
        if self.on_message is not None:
            self.on_message(message)

    def _put_latest_packet(self, packet: av.Packet | None) -> None:
        try:
            self._queue.put_nowait(packet)
            return
        except asyncio.QueueFull:
            pass

        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(packet)

    def get_capture_fps(self) -> float:
        if len(self._packet_times) < 2:
            return float(self.fps)
        elapsed = self._packet_times[-1] - self._packet_times[0]
        if elapsed <= 0:
            return float(self.fps)
        return (len(self._packet_times) - 1) / elapsed


@dataclass(slots=True)
class PeerState:
    peer_id: str
    pc: RTCPeerConnection
    connected_at: float = field(default_factory=time.time)


class HostStreamer:
    def __init__(
        self,
        config: HostSessionConfig,
        *,
        on_preview: Callable[[object], None] | None = None,
        on_stats: StatsCallback | None = None,
        on_toast: TextCallback | None = None,
        on_error: TextCallback | None = None,
    ) -> None:
        self.config = config
        self.on_preview = on_preview
        self.on_stats = on_stats
        self.on_toast = on_toast
        self.on_error = on_error

        self.capture_worker: ScreenCaptureWorker | None = None
        self.video_track: MediaStreamTrack | None = None
        self.audio_track: CompositeAudioTrack | None = None
        self.video_relay = MediaRelay()
        self.audio_relay = MediaRelay()
        self.signaling: HostSignalingServer | None = None
        self.relay_signaling: RelayHostSignalingClient | None = None
        ensure_webrtc_video_codecs_registered()
        self.encoder = detect_best_encoder_for_format(config.video_codec)
        self.quality_profile = resolve_quality_profile(config.resolution_label, config.quality)
        self.relay_url = configured_signaling_relay_url()
        self._relay_host_url: str | None = self.relay_url
        self._relay_join_url: str | None = self.relay_url
        self._embedded_relay: EmbeddedRelayServer | None = None
        self._quick_tunnel: QuickTunnelProcess | None = None
        self._quick_tunnel_task: asyncio.Task[str] | None = None
        self._quick_tunnel_error: str | None = None
        self._public_join_info: PublicJoinInfo | None = None
        self._port_mapping: PortMappingLease | None = None
        self._use_ffmpeg_packet_pipeline = False
        self._use_direct_desktop_capture = False
        self._peers: dict[str, PeerState] = {}
        self._stats_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        if self.encoder.video_codec == "h264" and (self.config.frame_width * self.config.frame_height) > (1920 * 1080):
            fallback_width, fallback_height = scale_to_fit(
                self.config.monitor_region["width"],
                self.config.monitor_region["height"],
                1920,
                1080,
            )
            if (fallback_width, fallback_height) != (self.config.frame_width, self.config.frame_height):
                self.config.frame_width = fallback_width
                self.config.frame_height = fallback_height
                self.config.resolution_label = "1080p"
                self.quality_profile = resolve_quality_profile("1080p", self.config.quality)
                self._safe_emit_toast(
                    "The current H.264 WebRTC runtime cannot deliver resolutions above 1080p reliably. Streaming switched to 1080p."
                )

        self._use_ffmpeg_packet_pipeline = (
            self.encoder.codec in {"h264_nvenc", "hevc_nvenc"}
            and self.encoder.ffmpeg_codec_available
            and self.encoder.ffmpeg_path is not None
        )
        self._use_direct_desktop_capture = (
            self._use_ffmpeg_packet_pipeline
            and ffmpeg_supports_filter(self.encoder.ffmpeg_path, "ddagrab")
        )

        for note in self.encoder.notes:
            self._safe_emit_toast(note)

        if not self._use_ffmpeg_packet_pipeline:
            try:
                apply_runtime_video_profile(
                    self.encoder,
                    width=self.config.frame_width,
                    height=self.config.frame_height,
                    fps=self.config.fps,
                    quality_profile=self.quality_profile,
                    on_message=self._safe_emit_toast,
                )
            except Exception as exc:
                self._safe_emit_toast(f"Encoder optimizer warning: {exc}")

        preview_fps = self.config.fps if not self._use_direct_desktop_capture else min(12, self.config.fps)
        preview_size = (
            (self.config.frame_width, self.config.frame_height)
            if not self._use_direct_desktop_capture
            else scale_to_fit(
                self.config.monitor_region["width"],
                self.config.monitor_region["height"],
                960,
                540,
            )
        )
        self.capture_worker = ScreenCaptureWorker(
            self.config.monitor_region,
            target_size=preview_size,
            fps=preview_fps,
            on_preview=self._safe_emit_preview,
            on_toast=self._safe_emit_toast,
            allow_auto_downscale=not self._use_ffmpeg_packet_pipeline,
            prefer_raw_bgra=self._use_ffmpeg_packet_pipeline and not self._use_direct_desktop_capture,
        )
        self.capture_worker.start()

        if self._use_direct_desktop_capture:
            self.video_track = FFmpegDesktopCaptureTrack(
                selection=self.encoder,
                monitor_index=self.config.monitor_index,
                input_width=self.config.monitor_region["width"],
                input_height=self.config.monitor_region["height"],
                width=self.config.frame_width,
                height=self.config.frame_height,
                fps=self.config.fps,
                quality_profile=self.quality_profile,
                include_cursor=False,
                on_message=self._safe_emit_toast,
            )
            self._safe_emit_toast(f"Streaming {self.encoder.transport_codec.split('/')[-1]} through FFmpeg desktop duplication with NVENC.")
        elif self._use_ffmpeg_packet_pipeline:
            self.video_track = FFmpegPacketTrack(
                self.capture_worker,
                selection=self.encoder,
                input_width=self.config.monitor_region["width"],
                input_height=self.config.monitor_region["height"],
                width=self.config.frame_width,
                height=self.config.frame_height,
                fps=self.config.fps,
                quality_profile=self.quality_profile,
                on_message=self._safe_emit_toast,
            )
            self._safe_emit_toast(f"Streaming {self.encoder.transport_codec.split('/')[-1]} through the bundled FFmpeg pipeline.")
        else:
            self.video_track = SharedScreenTrack(self.capture_worker)

        if self.config.share_microphone or self.config.share_system_audio:
            try:
                self.audio_track = CompositeAudioTrack(
                    capture_system_audio=self.config.share_system_audio,
                    capture_microphone=self.config.share_microphone,
                    on_message=self._safe_emit_toast,
                )
            except Exception as exc:
                self.audio_track = None
                self._safe_emit_toast(str(exc))

        self.signaling = HostSignalingServer(
            host="0.0.0.0",
            port=self.config.signaling_port,
            pin=self.config.pin,
            offer_handler=self._handle_offer,
        )
        await self.signaling.start()
        if self.relay_url:
            self.relay_signaling = RelayHostSignalingClient(
                self.relay_url,
                self.config.relay_session_id,
                self.config.pin,
                self._handle_offer,
                on_status=self._safe_emit_toast,
            )
            relay_connected = await self.relay_signaling.start()
            if relay_connected:
                self._safe_emit_toast("Internet relay signaling is ready.")
            else:
                self._safe_emit_toast(
                    "Internet relay signaling is still connecting. LAN viewers can join immediately."
                )
        else:
            try:
                self._embedded_relay = EmbeddedRelayServer()
                self._relay_host_url = await self._embedded_relay.start()
                self.relay_signaling = RelayHostSignalingClient(
                    self._relay_host_url,
                    self.config.relay_session_id,
                    self.config.pin,
                    self._handle_offer,
                    on_status=self._safe_emit_toast,
                )
                relay_connected = await self.relay_signaling.start()
                if relay_connected:
                    self._safe_emit_toast("Preparing an outbound public relay for internet viewers...")
                else:
                    self._safe_emit_toast("Preparing the local relay before publishing an internet join code...")
                self._quick_tunnel = QuickTunnelProcess(on_status=self._safe_emit_toast)
                self._quick_tunnel_task = asyncio.create_task(
                    asyncio.to_thread(self._quick_tunnel.start, self._relay_host_url)
                )
            except Exception as exc:
                self._quick_tunnel_error = str(exc)
                self._safe_emit_toast(
                    f"Automatic public relay setup failed. Falling back to direct internet detection: {exc}"
                )
        self._safe_emit_toast(f"Encoder selected: {self.encoder.display_name}")

        self._stats_task = asyncio.create_task(self._stats_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._stats_task is not None:
            self._stats_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stats_task
            self._stats_task = None

        if self.signaling is not None:
            await self.signaling.stop()
            self.signaling = None
        if self.relay_signaling is not None:
            await self.relay_signaling.stop()
            self.relay_signaling = None
        if self._quick_tunnel_task is not None:
            self._quick_tunnel_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._quick_tunnel_task
            self._quick_tunnel_task = None
        if self._quick_tunnel is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._quick_tunnel.stop)
            self._quick_tunnel = None
        if self._embedded_relay is not None:
            await self._embedded_relay.stop()
            self._embedded_relay = None
        self._relay_host_url = self.relay_url
        self._relay_join_url = self.relay_url
        self._quick_tunnel_error = None
        if self._port_mapping is not None:
            lease = self._port_mapping
            self._port_mapping = None
            with suppress(Exception):
                await asyncio.to_thread(lease.release)
        self._public_join_info = None

        peers = list(self._peers.items())
        self._peers.clear()
        for _, state in peers:
            await state.pc.close()

        if self.audio_track is not None:
            self.audio_track.stop()
            self.audio_track = None

        if self.capture_worker is not None:
            self.capture_worker.stop()
            self.capture_worker = None
        if self.video_track is not None:
            with suppress(Exception):
                self.video_track.stop()
        self.video_track = None
        # Give relay tasks a chance to observe the ended source track.
        await asyncio.sleep(0)

    async def resolve_public_join_info(self) -> PublicJoinInfo:
        if not self._running:
            return PublicJoinInfo(
                ready=False,
                mode="unavailable",
                join_code=None,
                summary="Host session is not live",
                detail="Start sharing before generating an internet join code.",
                endpoint_text="Session not running",
            )

        if self.relay_url:
            self._public_join_info = resolve_relay_join_info(
                relay_url=self.relay_url,
                session_id=self.config.relay_session_id,
                pin=self.config.pin,
                relay_connected=self.relay_signaling.is_connected if self.relay_signaling else False,
            )
            return self._public_join_info

        if self._quick_tunnel_task is not None and self._quick_tunnel_task.done():
            try:
                self._relay_join_url = self._quick_tunnel_task.result().rstrip("/")
                self._quick_tunnel_error = None
            except asyncio.CancelledError:
                self._quick_tunnel_error = "Automatic public relay setup was cancelled."
            except Exception as exc:
                self._quick_tunnel_error = str(exc)
            finally:
                self._quick_tunnel_task = None

        if self._relay_join_url:
            self._public_join_info = resolve_relay_join_info(
                relay_url=self._relay_join_url,
                session_id=self.config.relay_session_id,
                pin=self.config.pin,
                relay_connected=self.relay_signaling.is_connected if self.relay_signaling else False,
            )
            return self._public_join_info

        if self._quick_tunnel_task is not None:
            return PublicJoinInfo(
                ready=False,
                mode="relay",
                join_code=None,
                summary="Preparing internet join code",
                detail=(
                    "The host is publishing an outbound signaling relay. "
                    "This avoids port forwarding and works on CGNAT networks once the public URL is ready."
                ),
                endpoint_text="Preparing outbound public endpoint",
            )

        if self._public_join_info is not None:
            if self._quick_tunnel_error:
                detail = f"{self._public_join_info.detail} Automatic relay setup failed: {self._quick_tunnel_error}"
                return PublicJoinInfo(
                    ready=self._public_join_info.ready,
                    mode=self._public_join_info.mode,
                    join_code=self._public_join_info.join_code,
                    summary=self._public_join_info.summary,
                    detail=detail,
                    endpoint_text=self._public_join_info.endpoint_text,
                )
            return self._public_join_info

        info, lease = await asyncio.to_thread(
            resolve_direct_join_info,
            internal_host_ip=self.config.host_ip,
            signaling_port=self.config.signaling_port,
            pin=self.config.pin,
            turn_enabled=len(ice_server_settings()) > 1,
        )
        if self._quick_tunnel_error:
            info = PublicJoinInfo(
                ready=info.ready,
                mode=info.mode,
                join_code=info.join_code,
                summary=info.summary,
                detail=f"{info.detail} Automatic relay setup failed: {self._quick_tunnel_error}",
                endpoint_text=info.endpoint_text,
            )
        self._public_join_info = info
        if lease is not None:
            self._port_mapping = lease
        return info

    async def _handle_offer(self, payload: dict[str, Any]) -> dict[str, str]:
        if not self._running or self.video_track is None:
            raise RuntimeError("The host is not accepting viewers right now.")

        offer = payload["offer"]
        pc = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[RTCIceServer(**settings) for settings in ice_server_settings()],
            )
        )
        peer_id = secrets.token_hex(4)
        self._peers[peer_id] = PeerState(peer_id=peer_id, pc=pc)

        @pc.on("connectionstatechange")
        def _on_connection_state() -> None:
            if pc.connectionState in {"closed", "failed", "disconnected"}:
                asyncio.create_task(self._drop_peer(peer_id))

        video_sender = pc.addTrack(self.video_relay.subscribe(self.video_track, buffered=False))
        self._prefer_video_codec(pc, video_sender)

        if self.audio_track is not None:
            pc.addTrack(self.audio_relay.subscribe(self.audio_track))

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        )
        answer = await pc.createAnswer()

        try:
            target_kbps = parse_bitrate_to_kbps(self.quality_profile.max_bitrate)
            sdp_lines = answer.sdp.splitlines()
            mangled_lines = []
            for line in sdp_lines:
                mangled_lines.append(line)
                if line.startswith("m=video"):
                    mangled_lines.append(f"b=AS:{target_kbps}")
            answer.sdp = "\r\n".join(mangled_lines) + "\r\n"
        except Exception as e:
            self._safe_emit_toast(f"SDP modification failed: {e}")

        await pc.setLocalDescription(answer)
        await wait_for_ice_complete(pc)

        self._safe_emit_stats()
        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def _drop_peer(self, peer_id: str) -> None:
        state = self._peers.pop(peer_id, None)
        if state is not None:
            with suppress(Exception):
                await state.pc.close()
        self._safe_emit_stats()

    async def _stats_loop(self) -> None:
        previous_bytes: dict[str, int] = {}
        while self._running:
            await asyncio.sleep(1.0)

            active_states = [state for state in self._peers.values() if state.pc.connectionState != "closed"]
            total_bitrate = 0.0
            latencies: list[float] = []

            for state in active_states:
                try:
                    report = await state.pc.getStats()
                except Exception:
                    continue

                for stat in report.values():
                    stat_type = _stat_value(stat, "type")
                    if stat_type == "outbound-rtp" and _stat_value(stat, "kind") == "video":
                        key = f"{state.peer_id}:{_stat_value(stat, 'ssrc')}"
                        bytes_sent = int(_stat_value(stat, "bytesSent") or 0)
                        previous = previous_bytes.get(key)
                        if previous is not None:
                            total_bitrate += (bytes_sent - previous) * 8
                        previous_bytes[key] = bytes_sent
                    if stat_type in {"remote-inbound-rtp", "candidate-pair"}:
                        round_trip = _stat_value(stat, "roundTripTime")
                        if round_trip is not None:
                            latencies.append(float(round_trip) * 1000.0)

            capture_fps = 0.0
            if self.video_track is not None and hasattr(self.video_track, "get_capture_fps"):
                try:
                    capture_fps = float(getattr(self.video_track, "get_capture_fps")())
                except Exception:
                    capture_fps = 0.0
            elif self.capture_worker is not None:
                capture_fps = self.capture_worker.get_capture_fps()

            frame_width, frame_height = self.config.frame_width, self.config.frame_height

            if self.on_stats is not None:
                self.on_stats(
                    {
                        "viewers": len(active_states),
                        "bitrate_bps": total_bitrate,
                        "latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
                        "capture_fps": capture_fps,
                        "encoder": self.encoder.display_name,
                        "resolution": f"{frame_width}x{frame_height}",
                    }
                )

    def _prefer_video_codec(self, pc: RTCPeerConnection, sender: object) -> None:
        try:
            preferred = preferred_video_capabilities([self.encoder.video_codec])
            for transceiver in pc.getTransceivers():
                if transceiver.sender == sender and preferred:
                    transceiver.setCodecPreferences(preferred)
                    break
        except Exception:
            pass

    def _safe_emit_preview(self, image: object) -> None:
        if self.on_preview is not None:
            self.on_preview(image)

    def _safe_emit_toast(self, message: str) -> None:
        if self.on_toast is not None:
            self.on_toast(message)

    def _safe_emit_stats(self) -> None:
        if self.on_stats is not None:
            self.on_stats(
                {
                    "viewers": len(self._peers),
                    "bitrate_bps": 0.0,
                    "latency_ms": 0.0,
                    "capture_fps": (
                        float(getattr(self.video_track, "get_capture_fps")())
                        if self.video_track is not None and hasattr(self.video_track, "get_capture_fps")
                        else self.capture_worker.get_capture_fps() if self.capture_worker else 0.0
                    ),
                    "encoder": self.encoder.display_name,
                    "resolution": f"{self.config.frame_width}x{self.config.frame_height}",
                }
            )
