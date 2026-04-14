from __future__ import annotations

import queue
import threading
import tkinter as tk
from concurrent.futures import Future
from contextlib import suppress
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageOps, ImageTk

from screenshare.network.session import DEFAULT_SIGNALING_PORT, format_bitrate
from screenshare.network.public_access import JoinCodeError, decode_join_code
from screenshare.stream.receiver import ViewerClient


def _enqueue_latest(target_queue: queue.Queue[tuple[str, Any]], event: tuple[str, Any]) -> None:
    while True:
        try:
            target_queue.put_nowait(event)
            return
        except queue.Full:
            with suppress(queue.Empty):
                target_queue.get_nowait()


class Tooltip:
    def __init__(self, widget: tk.Widget) -> None:
        self.widget = widget
        self.text = ""
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def set_text(self, text: str) -> None:
        self.text = text

    def _show(self, _event=None) -> None:
        if not self.text or self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        label = tk.Label(
            self.window,
            text=self.text,
            bg="#111827",
            fg="white",
            padx=10,
            pady=6,
            justify="left",
        )
        label.pack()
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + 24
        self.window.geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None


class ViewerView(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk, *, runtime, on_back, show_error) -> None:
        super().__init__(master, fg_color="transparent")
        self._shortcut_root = master
        self.runtime = runtime
        self.on_back = on_back
        self.show_error = show_error

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=6)
        self._client: ViewerClient | None = None
        self._closing = False
        self._image_ref: ImageTk.PhotoImage | None = None
        self._last_frame = None
        self._pending_frame = None
        self._frame_lock = threading.Lock()
        self._frame_dirty = False
        self._video_image_id: int | None = None
        self._placeholder_id: int | None = None
        self._fullscreen = False

        self.status_var = tk.StringVar(value="Enter an internet join code, or enter the host IP and session PIN.")
        self.latency_var = tk.StringVar(value="0 ms")
        self.bitrate_var = tk.StringVar(value="0 Mbps")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_layout()
        self._shortcut_root.bind_all("<F11>", lambda _event: self._toggle_fullscreen())
        self.after(16, self._poll_events)

    def cleanup(self) -> None:
        self._closing = True
        self._shortcut_root.unbind_all("<F11>")
        with self._frame_lock:
            self._pending_frame = None
            self._last_frame = None
            self._frame_dirty = False
        if self._client is not None:
            self.runtime.submit(self._client.disconnect())
            self._client = None

    def _build_layout(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=22)
        header.grid(row=0, column=0, padx=22, pady=(22, 14), sticky="ew")
        header.grid_columnconfigure(4, weight=1)

        ctk.CTkButton(header, text="Back", width=90, command=self._go_back).grid(row=0, column=0, padx=(18, 10), pady=18)

        self.join_code_entry = ctk.CTkEntry(header, width=260, placeholder_text="Internet Join Code")
        self.join_code_entry.grid(row=0, column=1, padx=10, pady=18)
        self.host_entry = ctk.CTkEntry(header, width=220, placeholder_text="Host IP")
        self.host_entry.grid(row=0, column=2, padx=10, pady=18)
        self.host_entry.insert(0, "127.0.0.1")
        self.pin_entry = ctk.CTkEntry(header, width=150, placeholder_text="6-digit PIN")
        self.pin_entry.grid(row=0, column=3, padx=10, pady=18)

        ctk.CTkButton(header, text="Connect", width=110, command=self._connect).grid(row=0, column=4, padx=(10, 8), pady=18, sticky="e")
        ctk.CTkButton(
            header,
            text="Disconnect",
            width=110,
            fg_color="#8b1e3f",
            hover_color="#6d1631",
            command=self._disconnect,
        ).grid(row=0, column=5, padx=(8, 8), pady=18)
        ctk.CTkButton(header, text="Fullscreen", width=110, command=self._toggle_fullscreen).grid(
            row=0, column=6, padx=(8, 18), pady=18
        )

        body = ctk.CTkFrame(self, corner_radius=22)
        body.grid(row=1, column=0, padx=22, pady=(0, 22), sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        info_row = ctk.CTkFrame(body, fg_color="transparent")
        info_row.grid(row=0, column=0, padx=18, pady=(18, 10), sticky="ew")
        info_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(info_row, textvariable=self.status_var, text_color="#9aa6b2").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(info_row, text="Latency").grid(row=0, column=1, padx=(12, 6))
        ctk.CTkLabel(info_row, textvariable=self.latency_var, font=ctk.CTkFont(weight="bold")).grid(row=0, column=2)
        ctk.CTkLabel(info_row, text="Bitrate").grid(row=0, column=3, padx=(18, 6))
        ctk.CTkLabel(info_row, textvariable=self.bitrate_var, font=ctk.CTkFont(weight="bold")).grid(row=0, column=4)
        ctk.CTkLabel(info_row, text="Volume").grid(row=0, column=5, padx=(18, 6))

        self.volume_slider = ctk.CTkSlider(info_row, from_=0, to=150, number_of_steps=150, width=140, command=self._set_volume)
        self.volume_slider.grid(row=0, column=6, padx=(6, 12))
        self.volume_slider.set(100)

        self.quality_canvas = tk.Canvas(info_row, width=18, height=18, highlightthickness=0, bg="#2b3038")
        self.quality_canvas.grid(row=0, column=7, padx=(12, 0))
        self.quality_dot = self.quality_canvas.create_oval(3, 3, 15, 15, fill="#f2c94c", outline="")
        self.quality_tooltip = Tooltip(self.quality_canvas)
        self.quality_tooltip.set_text("Yellow: waiting for transport stats.")

        video_shell = ctk.CTkFrame(body, corner_radius=18, fg_color="#050b14")
        video_shell.grid(row=1, column=0, padx=18, pady=(0, 18), sticky="nsew")
        video_shell.grid_rowconfigure(0, weight=1)
        video_shell.grid_columnconfigure(0, weight=1)

        self.video_canvas = tk.Canvas(video_shell, highlightthickness=0, bg="#050b14")
        self.video_canvas.grid(row=0, column=0, sticky="nsew")
        self.video_canvas.bind("<Configure>", lambda _event: self._render_latest_frame(force=True))

        self._placeholder_id = self.video_canvas.create_text(
            500,
            280,
            text="Remote video appears here after connection.",
            fill="#c7d5e0",
            font=("Segoe UI", 18),
        )

    def _connect(self) -> None:
        join_code = self.join_code_entry.get().strip()
        host = self.host_entry.get().strip()
        pin = self.pin_entry.get().strip()
        port = DEFAULT_SIGNALING_PORT

        if join_code:
            try:
                target = decode_join_code(join_code)
            except JoinCodeError as exc:
                self.show_error("Invalid join code", str(exc))
                return
            host = target.host
            pin = target.pin
            port = target.port
            self.host_entry.delete(0, "end")
            self.host_entry.insert(0, host)
            self.pin_entry.delete(0, "end")
            self.pin_entry.insert(0, pin)
        elif not host or not pin:
            self.show_error("Missing fields", "Enter an internet join code, or enter both the host IP address and session PIN.")
            return

        if self._client is None:
            self._client = ViewerClient(
                on_frame=self._store_latest_frame,
                on_stats=lambda stats: _enqueue_latest(self._events, ("stats", stats)),
                on_status=lambda text: _enqueue_latest(self._events, ("status", text)),
                on_error=lambda text: _enqueue_latest(self._events, ("error", text)),
            )
            self._client.set_volume(self.volume_slider.get() / 100.0)

        self.status_var.set(f"Connecting to {host}:{port}...")
        future = self.runtime.submit(self._client.connect(host, pin, port))
        future.add_done_callback(lambda done: _enqueue_latest(self._events, ("connect_done", done)))

    def _disconnect(self) -> None:
        if self._client is None:
            return
        future = self.runtime.submit(self._client.disconnect())
        future.add_done_callback(lambda done: _enqueue_latest(self._events, ("disconnect_done", done)))

    def _poll_events(self) -> None:
        if self._closing:
            return

        try:
            while True:
                kind, payload = self._events.get_nowait()
                try:
                    if kind == "stats":
                        self._update_stats(payload)
                    elif kind == "status":
                        self.status_var.set(str(payload))
                    elif kind == "error":
                        self.status_var.set(str(payload))
                        self.show_error("Viewer error", str(payload))
                    elif kind == "connect_done":
                        self._handle_future(payload, "Unable to join session")
                    elif kind == "disconnect_done":
                        self._handle_future(payload, "Unable to disconnect cleanly")
                except Exception as exc:
                    self.status_var.set(f"Viewer UI error: {exc}")
                    self.show_error("Viewer UI error", str(exc))
        except queue.Empty:
            pass

        self._render_latest_frame()
        self.after(16, self._poll_events)

    def _handle_future(self, future: Future[Any], title: str) -> None:
        try:
            future.result()
        except Exception as exc:
            self.status_var.set(str(exc))
            self.show_error(title, str(exc))

    def _update_stats(self, stats: dict[str, Any]) -> None:
        self.latency_var.set(f"{float(stats.get('latency_ms', 0.0)):.0f} ms")
        self.bitrate_var.set(format_bitrate(float(stats.get("bitrate_bps", 0.0))))
        self.quality_canvas.itemconfigure(self.quality_dot, fill=str(stats.get("quality_color", "#f2c94c")))
        self.quality_tooltip.set_text(str(stats.get("quality_text", "")))

    def _store_latest_frame(self, image) -> None:
        with self._frame_lock:
            self._pending_frame = image
            self._frame_dirty = True

    def _render_latest_frame(self, *, force: bool = False) -> None:
        with self._frame_lock:
            if self._pending_frame is not None:
                self._last_frame = self._pending_frame
                self._pending_frame = None
            if not force and not self._frame_dirty:
                return
            frame = self._last_frame
            self._frame_dirty = False

        if frame is None:
            return

        width = max(self.video_canvas.winfo_width(), 10)
        height = max(self.video_canvas.winfo_height(), 10)
        source = frame.to_image() if hasattr(frame, "to_image") else frame
        rendered = ImageOps.contain(source, (width, height), Image.Resampling.BILINEAR)
        self._image_ref = ImageTk.PhotoImage(rendered)
        if self._placeholder_id is not None:
            self.video_canvas.delete(self._placeholder_id)
            self._placeholder_id = None
        if self._video_image_id is None:
            self._video_image_id = self.video_canvas.create_image(width // 2, height // 2, image=self._image_ref)
        else:
            self.video_canvas.coords(self._video_image_id, width // 2, height // 2)
            self.video_canvas.itemconfigure(self._video_image_id, image=self._image_ref)

    def _set_volume(self, value: float) -> None:
        if self._client is not None:
            self._client.set_volume(float(value) / 100.0)

    def _toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.winfo_toplevel().attributes("-fullscreen", self._fullscreen)

    def _go_back(self) -> None:
        if self._client is not None:
            self._disconnect()
        self.on_back()
