from __future__ import annotations

from dataclasses import dataclass

import mss


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    label: str
    width: int
    height: int
    left: int
    top: int

    @property
    def region(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


RESOLUTION_PRESETS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("4K", (3840, 2160)),
    ("1080p", (1920, 1080)),
    ("720p", (1280, 720)),
)


def list_monitors() -> list[MonitorInfo]:
    monitors: list[MonitorInfo] = []
    with mss.mss() as sct:
        for index, monitor in enumerate(sct.monitors[1:], start=1):
            monitors.append(
                MonitorInfo(
                    index=index,
                    label=f"Monitor {index} ({monitor['width']}x{monitor['height']})",
                    width=int(monitor["width"]),
                    height=int(monitor["height"]),
                    left=int(monitor["left"]),
                    top=int(monitor["top"]),
                )
            )
    return monitors


def available_resolution_labels(monitor: MonitorInfo) -> list[str]:
    labels: list[str] = []
    for label, (width, height) in RESOLUTION_PRESETS:
        if monitor.width >= width and monitor.height >= height:
            labels.append(label)
    if not labels:
        labels.append("720p")
    return labels


def scale_to_fit(source_width: int, source_height: int, max_width: int, max_height: int) -> tuple[int, int]:
    ratio = min(max_width / source_width, max_height / source_height)
    width = max(2, int(source_width * ratio))
    height = max(2, int(source_height * ratio))
    return make_even(width), make_even(height)


def resolve_resolution(monitor: MonitorInfo, label: str) -> tuple[int, int]:
    if label == "4K":
        return scale_to_fit(monitor.width, monitor.height, 3840, 2160)
    if label == "1080p":
        return scale_to_fit(monitor.width, monitor.height, 1920, 1080)
    return scale_to_fit(monitor.width, monitor.height, 1280, 720)


def make_even(value: int) -> int:
    return value if value % 2 == 0 else value - 1
