from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image

from screenshare.utils.resolution import scale_to_fit

try:
    import mss
except Exception:  # pragma: no cover - optional import fallback
    mss = None


PreviewCallback = Callable[[Image.Image], None]
TextCallback = Callable[[str], None]


@dataclass(slots=True)
class CapturedFrame:
    array: np.ndarray | None
    timestamp: float
    sequence: int
    width: int
    height: int
    pixel_format: str = "rgb24"
    raw_bytes: bytes | None = None


class ScreenCaptureWorker:
    """Captures the selected monitor on a dedicated thread."""

    _MSS_FAILURE_THRESHOLD = 3

    def __init__(
        self,
        monitor_region: dict[str, int],
        target_size: tuple[int, int],
        fps: int,
        on_preview: PreviewCallback | None = None,
        on_toast: TextCallback | None = None,
        allow_auto_downscale: bool = True,
        prefer_raw_bgra: bool = False,
    ) -> None:
        self.monitor_region = monitor_region
        self.target_size = target_size
        self.fps = fps
        self.on_preview = on_preview
        self.on_toast = on_toast
        self.allow_auto_downscale = allow_auto_downscale
        self.prefer_raw_bgra = prefer_raw_bgra

        self._lock = threading.Condition()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_frame: CapturedFrame | None = None
        self._sequence = 0
        self._preview_interval = 0.20
        self._last_preview_at = 0.0
        self._capture_times: deque[float] = deque(maxlen=max(12, fps // 2))
        self._slow_frame_count = 0
        self._mss_failure_count = 0
        self._downscaled = False
        self._backend = "mss" if mss is not None else "pyautogui"

    @property
    def backend_name(self) -> str:
        return self._backend

    @property
    def current_size(self) -> tuple[int, int]:
        return self.target_size

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._run,
            name="screenshare-capture",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def get_latest_frame(self, last_sequence: int | None = None, timeout: float = 2.0) -> CapturedFrame:
        end_time = time.time() + timeout
        with self._lock:
            while self._running.is_set():
                if self._latest_frame is not None and (
                    last_sequence is None or self._latest_frame.sequence > last_sequence
                ):
                    return self._latest_frame
                remaining = end_time - time.time()
                if remaining <= 0:
                    break
                self._lock.wait(timeout=remaining)

            if self._latest_frame is None:
                raise TimeoutError("No screen frame is available yet.")
            return self._latest_frame

    def get_capture_fps(self) -> float:
        if not self._capture_times:
            return 0.0
        average = sum(self._capture_times) / len(self._capture_times)
        return 1.0 / average if average > 0 else 0.0

    def _run(self) -> None:
        frame_interval = 1.0 / max(self.fps, 1)
        next_tick = time.perf_counter()
        sct = None

        try:
            if self._backend == "mss":
                sct = mss.mss()

            while self._running.is_set():
                started = time.perf_counter()
                try:
                    frame_array, frame_width, frame_height, pixel_format, raw_bytes = self._grab_frame(sct)
                    self._mss_failure_count = 0
                except Exception:
                    if self._backend == "mss":
                        self._mss_failure_count += 1
                        if self._mss_failure_count < self._MSS_FAILURE_THRESHOLD:
                            time.sleep(min(0.02 * self._mss_failure_count, frame_interval))
                            next_tick = time.perf_counter()
                            continue

                        self._backend = "pyautogui"
                        self._mss_failure_count = 0
                        if self.on_toast is not None:
                            self.on_toast("mss screen capture failed repeatedly. Falling back to pyautogui.")
                        frame_array, frame_width, frame_height, pixel_format, raw_bytes = self._grab_frame(None)
                    else:
                        raise

                if (
                    frame_array is not None
                    and (frame_array.shape[1] != self.target_size[0] or frame_array.shape[0] != self.target_size[1])
                ):
                    frame_array = self._resize_frame_array(frame_array)
                    frame_width = frame_array.shape[1]
                    frame_height = frame_array.shape[0]

                timestamp = time.time()

                with self._lock:
                    self._sequence += 1
                    self._latest_frame = CapturedFrame(
                        array=frame_array,
                        timestamp=timestamp,
                        sequence=self._sequence,
                        width=frame_width,
                        height=frame_height,
                        pixel_format=pixel_format,
                        raw_bytes=raw_bytes,
                    )
                    self._lock.notify_all()

                now = time.perf_counter()
                if self.on_preview and (now - self._last_preview_at) >= self._preview_interval:
                    preview = self._build_preview_image(frame_array, frame_width, frame_height, pixel_format, raw_bytes)
                    preview.thumbnail((380, 220), Image.Resampling.LANCZOS)
                    self.on_preview(preview)
                    self._last_preview_at = now

                duration = time.perf_counter() - started
                self._capture_times.append(duration)
                if duration > frame_interval * 1.10:
                    self._slow_frame_count += 1
                else:
                    self._slow_frame_count = 0
                if self.allow_auto_downscale:
                    self._maybe_downscale(frame_interval)

                next_tick += frame_interval
                sleep_for = next_tick - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.perf_counter()
        finally:
            if sct is not None:
                sct.close()

    def _grab_frame(self, sct: mss.mss | None) -> tuple[np.ndarray | None, int, int, str, bytes | None]:
        if self._backend == "mss" and sct is not None:
            raw = sct.grab(self.monitor_region)
            if self.prefer_raw_bgra:
                return None, raw.width, raw.height, "bgra", raw.bgra
            rgba = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)
            return rgba[:, :, :3][:, :, ::-1].copy(), raw.width, raw.height, "rgb24", None

        import pyautogui

        image = pyautogui.screenshot(
            region=(
                self.monitor_region["left"],
                self.monitor_region["top"],
                self.monitor_region["width"],
                self.monitor_region["height"],
            )
        )
        array = np.asarray(image.convert("RGB"), dtype=np.uint8)
        return array, array.shape[1], array.shape[0], "rgb24", None

    def _resize_frame_array(self, frame_array: np.ndarray) -> np.ndarray:
        image = Image.fromarray(frame_array, "RGB")
        resized = image.resize(self.target_size, Image.Resampling.BILINEAR)
        return np.asarray(resized, dtype=np.uint8)

    def _build_preview_image(
        self,
        frame_array: np.ndarray | None,
        width: int,
        height: int,
        pixel_format: str,
        raw_bytes: bytes | None,
    ) -> Image.Image:
        if frame_array is not None:
            return Image.fromarray(frame_array, "RGB")
        if pixel_format == "bgra" and raw_bytes is not None:
            return Image.frombytes("RGB", (width, height), raw_bytes, "raw", "BGRX")
        raise RuntimeError("No preview data is available for the current frame.")

    def _maybe_downscale(self, frame_interval: float) -> None:
        if self._downscaled:
            return
        width, height = self.target_size
        if width * height <= 1920 * 1080:
            return
        pressure_threshold = max(4, self.fps // 12)
        if self._slow_frame_count < pressure_threshold and len(self._capture_times) < max(
            8, self._capture_times.maxlen // 2
        ):
            return
        if self._slow_frame_count < pressure_threshold:
            average_time = sum(self._capture_times) / len(self._capture_times)
            if average_time <= frame_interval * 1.10:
                return

        self.target_size = scale_to_fit(
            self.monitor_region["width"],
            self.monitor_region["height"],
            1920,
            1080,
        )
        self._downscaled = True
        self._capture_times.clear()
        self._slow_frame_count = 0
        if self.on_toast is not None:
            self.on_toast("4K capture is too slow on this machine. Streaming switched to 1080p.")
