from __future__ import annotations

import asyncio
import queue
import sys
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

import numpy as np
import sounddevice as sd
from aiortc import MediaStreamTrack
from av import AudioFrame


TextCallback = Callable[[str], None]


@dataclass(slots=True)
class AudioInputSpec:
    label: str
    device: int
    channels: int
    extra_settings: object | None = None


def _stereoize(samples: np.ndarray, channels: int) -> np.ndarray:
    if samples.ndim == 1:
        samples = samples[:, np.newaxis]
    if samples.shape[1] == 1 and channels == 2:
        samples = np.repeat(samples, 2, axis=1)
    elif samples.shape[1] > channels:
        samples = samples[:, :channels]
    elif samples.shape[1] < channels:
        pad = np.zeros((samples.shape[0], channels - samples.shape[1]), dtype=np.float32)
        samples = np.concatenate([samples, pad], axis=1)
    return samples.astype(np.float32, copy=False)


def find_microphone_input(channels: int = 2) -> AudioInputSpec | None:
    try:
        default_input = sd.default.device[0]
        if default_input is None or default_input < 0:
            return None
        info = sd.query_devices(default_input)
        if int(info["max_input_channels"]) <= 0:
            return None
        return AudioInputSpec(
            label=str(info["name"]),
            device=int(default_input),
            channels=max(1, min(int(info["max_input_channels"]), channels)),
        )
    except Exception:
        return None


def find_system_audio_input(channels: int = 2) -> AudioInputSpec | None:
    try:
        devices = sd.query_devices()
        output_index = sd.default.device[1]
        hostapis = sd.query_hostapis()
    except Exception:
        return None

    if sys.platform.startswith("win") and hasattr(sd, "WasapiSettings"):
        try:
            if output_index is None or output_index < 0:
                return None
            info = devices[output_index]
            hostapi = hostapis[info["hostapi"]]
            if "WASAPI" not in str(hostapi["name"]).upper():
                return None
            return AudioInputSpec(
                label=f"{info['name']} (loopback)",
                device=int(output_index),
                channels=max(1, min(int(info["max_output_channels"]), channels)),
                extra_settings=sd.WasapiSettings(loopback=True),
            )
        except Exception:
            return None

    for index, device in enumerate(devices):
        name = str(device["name"]).lower()
        if int(device["max_input_channels"]) <= 0:
            continue
        if "monitor" in name or "loopback" in name or "blackhole" in name or "soundflower" in name:
            return AudioInputSpec(
                label=str(device["name"]),
                device=index,
                channels=max(1, min(int(device["max_input_channels"]), channels)),
            )
    return None


class CompositeAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(
        self,
        *,
        capture_system_audio: bool,
        capture_microphone: bool,
        sample_rate: int = 48_000,
        channels: int = 2,
        on_message: TextCallback | None = None,
    ) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = int(sample_rate * 0.02)
        self.on_message = on_message

        self._loop = asyncio.get_running_loop()
        self._timestamp = 0
        self._streams: list[sd.InputStream] = []
        self._queues: dict[str, asyncio.Queue[np.ndarray]] = {}

        if capture_system_audio:
            system_spec = find_system_audio_input(channels=channels)
            if system_spec is None:
                if on_message is not None:
                    on_message("System audio capture is unavailable. Streaming microphone only.")
            else:
                self._open_stream("system", system_spec)

        if capture_microphone:
            mic_spec = find_microphone_input(channels=channels)
            if mic_spec is None:
                if on_message is not None:
                    on_message("No microphone input is available on this machine.")
            else:
                self._open_stream("mic", mic_spec)

        if not self._streams:
            raise RuntimeError("No audio capture device is available for the selected options.")

    async def recv(self) -> AudioFrame:
        mix = np.zeros((self.frame_samples, self.channels), dtype=np.float32)
        contributors = 0

        for queue_name, source_queue in self._queues.items():
            timeout = 0.04 if contributors == 0 else 0.0
            try:
                chunk = await asyncio.wait_for(source_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if contributors == 0 and queue_name == next(iter(self._queues)):
                    await asyncio.sleep(self.frame_samples / self.sample_rate)
                continue
            except asyncio.QueueEmpty:
                continue

            chunk = _stereoize(chunk, self.channels)
            if chunk.shape[0] < self.frame_samples:
                pad = np.zeros((self.frame_samples - chunk.shape[0], self.channels), dtype=np.float32)
                chunk = np.concatenate([chunk, pad], axis=0)
            elif chunk.shape[0] > self.frame_samples:
                chunk = chunk[: self.frame_samples]

            mix += chunk
            contributors += 1

        if contributors > 1:
            mix /= contributors

        pcm = np.clip(mix, -1.0, 1.0)
        int16 = (pcm * 32767).astype(np.int16).T

        frame = AudioFrame.from_ndarray(int16, format="s16", layout="stereo")
        frame.sample_rate = self.sample_rate
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, self.sample_rate)
        self._timestamp += self.frame_samples
        return frame

    def stop(self) -> None:
        for stream in self._streams:
            with suppress(Exception):
                stream.stop()
            with suppress(Exception):
                stream.close()
        self._streams.clear()
        super().stop()

    def _open_stream(self, queue_name: str, spec: AudioInputSpec) -> None:
        audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=8)
        self._queues[queue_name] = audio_queue

        def callback(indata: np.ndarray, frames: int, _time: object, status: sd.CallbackFlags) -> None:
            if status and self.on_message is not None:
                self.on_message(f"Audio warning from {spec.label}: {status}")

            payload = _stereoize(indata.copy(), self.channels)

            def enqueue() -> None:
                if audio_queue.full():
                    with suppress(asyncio.QueueEmpty):
                        audio_queue.get_nowait()
                audio_queue.put_nowait(payload)

            self._loop.call_soon_threadsafe(enqueue)

        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=spec.channels,
            device=spec.device,
            dtype="float32",
            blocksize=self.frame_samples,
            callback=callback,
            extra_settings=spec.extra_settings,
        )
        stream.start()
        self._streams.append(stream)


class AudioPlaybackBuffer:
    """Simple sounddevice-backed PCM playback for the viewer side."""

    def __init__(self, sample_rate: int = 48_000, channels: int = 2) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=96)
        self._leftover = np.zeros((0, channels), dtype=np.float32)
        self._volume = 1.0
        self._stream: sd.OutputStream | None = None

    def start(self) -> None:
        if self._stream is None:
            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                callback=self._callback,
                blocksize=int(self.sample_rate * 0.02),
            )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            with suppress(Exception):
                self._stream.stop()
            with suppress(Exception):
                self._stream.close()
            self._stream = None

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(volume, 1.5))

    def push_frame(self, frame: AudioFrame) -> None:
        data = frame.to_ndarray()
        if data.ndim == 1:
            data = data[np.newaxis, :]
        if data.shape[0] in (1, 2):
            data = data.T
        data = data.astype(np.float32)
        if frame.format.name.startswith("s16"):
            data /= 32768.0

        if data.shape[1] == 1 and self.channels == 2:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > self.channels:
            data = data[:, : self.channels]

        if self._queue.full():
            with suppress(queue.Empty):
                self._queue.get_nowait()
        self._queue.put_nowait(data)

    def _callback(self, outdata: np.ndarray, frames: int, _time: object, _status: sd.CallbackFlags) -> None:
        outdata[:] = 0
        written = 0

        while written < frames:
            if len(self._leftover) == 0:
                try:
                    self._leftover = self._queue.get_nowait()
                except queue.Empty:
                    break

            take = min(frames - written, len(self._leftover))
            outdata[written : written + take] = self._leftover[:take] * self._volume
            self._leftover = self._leftover[take:]
            written += take
