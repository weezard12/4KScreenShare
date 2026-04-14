from __future__ import annotations

import queue
import threading
import tkinter as tk
from concurrent.futures import Future
from contextlib import suppress
from typing import Any

import customtkinter as ctk
from PIL import ImageTk

from screenshare.network.session import (
    FPS_PRESETS,
    QUALITY_PRESETS,
    HostSessionConfig,
    VIDEO_CODEC_PRESETS,
    detect_local_ip,
    format_bitrate,
    generate_session_pin,
    has_turn_server_config,
)
from screenshare.network.public_access import PublicJoinInfo, resolve_public_join_info
from screenshare.stream.sender import HostStreamer
from screenshare.stream.video_codecs import normalize_video_codec
from screenshare.utils.resolution import available_resolution_labels, list_monitors, resolve_resolution


def _enqueue_latest(target_queue: queue.Queue[tuple[str, Any]], event: tuple[str, Any]) -> None:
    while True:
        try:
            target_queue.put_nowait(event)
            return
        except queue.Full:
            with suppress(queue.Empty):
                target_queue.get_nowait()


class HostView(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk, *, runtime, on_back, show_error) -> None:
        super().__init__(master, fg_color="transparent")
        self.runtime = runtime
        self.on_back = on_back
        self.show_error = show_error

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=10)
        self._service: HostStreamer | None = None
        self._closing = False
        self._preview_ref: ImageTk.PhotoImage | None = None
        self._toast_window: ctk.CTkToplevel | None = None

        self.pin = generate_session_pin()
        self.host_ip = detect_local_ip()
        self.monitors = list_monitors()
        if not self.monitors:
            raise RuntimeError("No display monitors were detected.")

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.status_var = tk.StringVar(value="Ready to host a session.")
        self.viewers_var = tk.StringVar(value="0")
        self.bitrate_var = tk.StringVar(value="0 Mbps")
        self.latency_var = tk.StringVar(value="0 ms")
        self.capture_fps_var = tk.StringVar(value="0.0 FPS")
        self.encoder_var = tk.StringVar(value="Pending")
        self.actual_resolution_var = tk.StringVar(value="Pending")
        self.public_code_var = tk.StringVar(value="Waiting for session start")
        self.public_endpoint_var = tk.StringVar(value="Start sharing to resolve")

        self._build_layout()
        self.after(60, self._poll_events)

    def cleanup(self) -> None:
        self._closing = True
        if self._service is not None:
            self.runtime.submit(self._service.stop())
            self._service = None

    def _build_layout(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=22)
        header.grid(row=0, column=0, columnspan=2, padx=22, pady=(22, 14), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        header.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(header, text="Back", width=90, command=self._go_back).grid(
            row=0, column=0, padx=18, pady=18, sticky="w"
        )
        ctk.CTkLabel(
            header,
            text="Host Session",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).grid(row=0, column=1, padx=12, pady=18, sticky="w")

        identity = ctk.CTkFrame(header, fg_color="#10283d", corner_radius=18)
        identity.grid(row=0, column=2, padx=18, pady=18, sticky="e")
        identity.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(identity, text=f"Session PIN  {self.pin}", font=ctk.CTkFont(size=22, weight="bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(14, 10), sticky="w"
        )
        ctk.CTkLabel(
            identity,
            text="Local test on same PC: 127.0.0.1",
            text_color="#c7d5e0",
        ).grid(row=1, column=0, columnspan=2, padx=20, pady=(0, 14), sticky="w")
        ctk.CTkLabel(
            identity,
            text="Internet Join Code",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#86efac",
        ).grid(row=2, column=0, columnspan=2, padx=20, pady=(0, 6), sticky="w")
        self.public_code_entry = ctk.CTkEntry(
            identity,
            width=290,
            textvariable=self.public_code_var,
            state="readonly",
        )
        self.public_code_entry.grid(row=3, column=0, padx=(20, 8), pady=(0, 8), sticky="ew")
        self.public_code_entry.bind("<Button-1>", self._select_join_code)
        self.copy_join_code_button = ctk.CTkButton(
            identity,
            text="Copy",
            width=74,
            height=34,
            state="disabled",
            command=self._copy_join_code,
        )
        self.copy_join_code_button.grid(row=3, column=1, padx=(0, 20), pady=(0, 8), sticky="e")
        ctk.CTkLabel(
            identity,
            textvariable=self.public_endpoint_var,
            text_color="#c7d5e0",
            wraplength=360,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, padx=20, pady=(0, 14), sticky="w")

        controls = ctk.CTkFrame(self, width=340, corner_radius=22)
        controls.grid(row=1, column=0, padx=(22, 12), pady=(0, 22), sticky="ns")
        controls.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(controls, text="Streaming Settings", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(22, 18), sticky="w"
        )

        ctk.CTkLabel(controls, text="Monitor").grid(row=1, column=0, padx=20, pady=(0, 6), sticky="w")
        self.monitor_menu = ctk.CTkComboBox(
            controls,
            values=[monitor.label for monitor in self.monitors],
            command=self._on_monitor_changed,
        )
        self.monitor_menu.grid(row=2, column=0, padx=20, pady=(0, 14), sticky="ew")
        self.monitor_menu.set(self.monitors[0].label)

        ctk.CTkLabel(controls, text="Resolution").grid(row=3, column=0, padx=20, pady=(0, 6), sticky="w")
        self.resolution_menu = ctk.CTkComboBox(controls, values=["4K", "1080p", "720p"])
        self.resolution_menu.grid(row=4, column=0, padx=20, pady=(0, 14), sticky="ew")

        ctk.CTkLabel(controls, text="Frames Per Second").grid(row=5, column=0, padx=20, pady=(0, 6), sticky="w")
        self.fps_menu = ctk.CTkComboBox(controls, values=[str(value) for value in FPS_PRESETS])
        self.fps_menu.grid(row=6, column=0, padx=20, pady=(0, 14), sticky="ew")
        self.fps_menu.set("30")

        ctk.CTkLabel(controls, text="Quality Preset").grid(row=7, column=0, padx=20, pady=(0, 6), sticky="w")
        self.quality_menu = ctk.CTkComboBox(controls, values=list(QUALITY_PRESETS))
        self.quality_menu.grid(row=8, column=0, padx=20, pady=(0, 14), sticky="ew")
        self.quality_menu.set("Balanced")

        ctk.CTkLabel(controls, text="Video Encoder Format").grid(row=9, column=0, padx=20, pady=(0, 6), sticky="w")
        self.video_codec_menu = ctk.CTkComboBox(controls, values=list(VIDEO_CODEC_PRESETS))
        self.video_codec_menu.grid(row=10, column=0, padx=20, pady=(0, 14), sticky="ew")
        self.video_codec_menu.set("H.264")

        self.system_audio_switch = ctk.CTkSwitch(controls, text="Share system audio")
        self.system_audio_switch.grid(row=11, column=0, padx=20, pady=(6, 10), sticky="w")
        self.microphone_switch = ctk.CTkSwitch(controls, text="Share microphone")
        self.microphone_switch.grid(row=12, column=0, padx=20, pady=(0, 16), sticky="w")

        action_row = ctk.CTkFrame(controls, fg_color="transparent")
        action_row.grid(row=13, column=0, padx=20, pady=(6, 20), sticky="ew")
        action_row.grid_columnconfigure((0, 1), weight=1)

        self.start_button = ctk.CTkButton(action_row, text="Start Sharing", height=46, command=self._start_host)
        self.start_button.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.stop_button = ctk.CTkButton(
            action_row,
            text="Stop Sharing",
            height=46,
            fg_color="#8b1e3f",
            hover_color="#6d1631",
            state="disabled",
            command=self._stop_host,
        )
        self.stop_button.grid(row=0, column=1, padx=(8, 0), sticky="ew")

        ctk.CTkLabel(
            controls,
            textvariable=self.status_var,
            wraplength=300,
            justify="left",
            text_color="#9aa6b2",
        ).grid(row=14, column=0, padx=20, pady=(0, 22), sticky="w")

        surface = ctk.CTkFrame(self, corner_radius=22)
        surface.grid(row=1, column=1, padx=(12, 22), pady=(0, 22), sticky="nsew")
        surface.grid_rowconfigure(1, weight=1)
        surface.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(surface, text="Live Thumbnail Preview", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(20, 12), sticky="w"
        )

        preview_shell = ctk.CTkFrame(surface, corner_radius=18, fg_color="#0b1220")
        preview_shell.grid(row=1, column=0, padx=20, pady=(0, 16), sticky="nsew")
        preview_shell.grid_rowconfigure(0, weight=1)
        preview_shell.grid_columnconfigure(0, weight=1)

        self.preview_label = tk.Label(
            preview_shell,
            text="Preview appears here when sharing starts.",
            fg="#c7d5e0",
            bg="#0b1220",
            font=("Segoe UI", 14),
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        stats = ctk.CTkFrame(surface, corner_radius=18, fg_color="#101923")
        stats.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="ew")
        stats.grid_columnconfigure((0, 1, 2), weight=1)

        self._stat_card(stats, "Viewers", self.viewers_var).grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        self._stat_card(stats, "Bitrate", self.bitrate_var).grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        self._stat_card(stats, "Latency", self.latency_var).grid(row=0, column=2, padx=10, pady=10, sticky="ew")
        self._stat_card(stats, "Capture FPS", self.capture_fps_var).grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        self._stat_card(stats, "Encoder", self.encoder_var).grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        self._stat_card(stats, "Actual Resolution", self.actual_resolution_var).grid(row=1, column=2, padx=10, pady=10, sticky="ew")

        self._on_monitor_changed(self.monitors[0].label)

    def _stat_card(self, master: ctk.CTkFrame, title: str, value_var: tk.StringVar) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(master, corner_radius=16, fg_color="#162130")
        ctk.CTkLabel(frame, text=title, text_color="#9aa6b2").pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(frame, textvariable=value_var, font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=16, pady=(0, 14)
        )
        return frame

    def _selected_monitor(self):
        label = self.monitor_menu.get()
        return next(monitor for monitor in self.monitors if monitor.label == label)

    def _on_monitor_changed(self, _selection: str) -> None:
        monitor = self._selected_monitor()
        options = available_resolution_labels(monitor)
        self.resolution_menu.configure(values=options)
        self.resolution_menu.set(options[0])

    def _build_config(self) -> HostSessionConfig:
        monitor = self._selected_monitor()
        resolution_label = self.resolution_menu.get()
        width, height = resolve_resolution(monitor, resolution_label)
        return HostSessionConfig(
            pin=self.pin,
            host_ip=self.host_ip,
            monitor_index=monitor.index,
            monitor_label=monitor.label,
            monitor_region=monitor.region,
            resolution_label=resolution_label,
            frame_width=width,
            frame_height=height,
            fps=int(self.fps_menu.get()),
            quality=self.quality_menu.get(),
            share_system_audio=bool(self.system_audio_switch.get()),
            share_microphone=bool(self.microphone_switch.get()),
            video_codec=normalize_video_codec(self.video_codec_menu.get()),
        )

    def _start_host(self) -> None:
        if self._service is not None:
            self._show_toast("Stop the active session before changing settings.")
            return

        try:
            config = self._build_config()
        except Exception as exc:
            self.show_error("Invalid host settings", str(exc))
            return

        self.start_button.configure(state="disabled")
        self.status_var.set("Starting host session...")
        self._service = HostStreamer(
            config,
            on_preview=lambda image: _enqueue_latest(self._events, ("preview", image)),
            on_stats=lambda stats: _enqueue_latest(self._events, ("stats", stats)),
            on_toast=lambda text: _enqueue_latest(self._events, ("toast", text)),
            on_error=lambda text: _enqueue_latest(self._events, ("error", text)),
        )

        future = self.runtime.submit(self._service.start())
        future.add_done_callback(lambda done: _enqueue_latest(self._events, ("start_done", done)))

    def _stop_host(self) -> None:
        if self._service is None:
            return
        self.status_var.set("Stopping host session...")
        self.stop_button.configure(state="disabled")
        future = self.runtime.submit(self._service.stop())
        future.add_done_callback(lambda done: _enqueue_latest(self._events, ("stop_done", done)))

    def _poll_events(self) -> None:
        if self._closing:
            return

        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "preview":
                    self._update_preview(payload)
                elif kind == "stats":
                    self._update_stats(payload)
                elif kind == "toast":
                    self._show_toast(str(payload))
                elif kind == "error":
                    self.show_error("Host error", str(payload))
                elif kind == "start_done":
                    self._handle_start_done(payload)
                elif kind == "stop_done":
                    self._handle_stop_done(payload)
                elif kind == "public_info":
                    self._handle_public_info(payload)
        except queue.Empty:
            pass

        self.after(60, self._poll_events)

    def _handle_start_done(self, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as exc:
            self._service = None
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status_var.set("Host failed to start.")
            self.show_error("Unable to start sharing", str(exc))
            return

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Session is live. Share the internet join code or the LAN IP and PIN with viewers. For same-machine testing, join 127.0.0.1.")
        self.public_code_var.set("Resolving...")
        self.public_endpoint_var.set("Public signaling endpoint is being discovered.")
        self.copy_join_code_button.configure(state="disabled")
        self._resolve_public_join_async()

    def _handle_stop_done(self, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as exc:
            self.show_error("Unable to stop sharing", str(exc))
        self._service = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Session stopped.")
        self.viewers_var.set("0")
        self.bitrate_var.set("0 Mbps")
        self.latency_var.set("0 ms")
        self.public_code_var.set("Waiting for session start")
        self.public_endpoint_var.set("Start sharing to resolve")
        self.copy_join_code_button.configure(state="disabled")

    def _update_preview(self, image) -> None:
        self._preview_ref = ImageTk.PhotoImage(image=image)
        self.preview_label.configure(image=self._preview_ref, text="")

    def _update_stats(self, stats: dict[str, Any]) -> None:
        self.viewers_var.set(str(stats.get("viewers", 0)))
        self.bitrate_var.set(format_bitrate(float(stats.get("bitrate_bps", 0.0))))
        self.latency_var.set(f"{float(stats.get('latency_ms', 0.0)):.0f} ms")
        self.capture_fps_var.set(f"{float(stats.get('capture_fps', 0.0)):.1f} FPS")
        self.encoder_var.set(str(stats.get("encoder", "Pending")))
        self.actual_resolution_var.set(str(stats.get("resolution", "Pending")))

    def _show_toast(self, message: str) -> None:
        if self._toast_window is not None and self._toast_window.winfo_exists():
            self._toast_window.destroy()

        toast = ctk.CTkToplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(fg_color="#111827")
        ctk.CTkLabel(toast, text=message, wraplength=360, padx=18, pady=12).pack()
        self.update_idletasks()
        x = self.winfo_rootx() + self.winfo_width() - 420
        y = self.winfo_rooty() + 40
        toast.geometry(f"+{max(x, 20)}+{max(y, 20)}")
        toast.after(3200, toast.destroy)
        self._toast_window = toast

    def _resolve_public_join_async(self) -> None:
        if self._service is None:
            return

        config = self._service.config
        turn_enabled = has_turn_server_config()

        def _worker() -> None:
            info = resolve_public_join_info(
                pin=config.pin,
                signaling_port=config.signaling_port,
                turn_enabled=turn_enabled,
            )
            _enqueue_latest(self._events, ("public_info", info))

        threading.Thread(target=_worker, name="public-join-resolver", daemon=True).start()

    def _handle_public_info(self, info: PublicJoinInfo) -> None:
        if self._service is None:
            return
        if info.join_code:
            self.public_code_var.set(info.join_code)
            self.public_endpoint_var.set(f"Public endpoint  {info.public_host}:{info.signaling_port}")
            self.copy_join_code_button.configure(state="normal")
        else:
            self.public_code_var.set("Unavailable")
            self.public_endpoint_var.set(info.summary)
            self.copy_join_code_button.configure(state="disabled")
        if info.detail and not info.join_code:
            self._show_toast(info.detail)

    def _select_join_code(self, _event=None) -> None:
        self.public_code_entry.focus_set()
        self.public_code_entry.selection_range(0, "end")

    def _copy_join_code(self) -> None:
        join_code = self.public_code_var.get().strip()
        if not join_code or join_code in {"Waiting for session start", "Resolving...", "Unavailable"}:
            self._show_toast("The internet join code is not ready yet.")
            return
        root = self.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append(join_code)
        root.update_idletasks()
        self._show_toast("Internet join code copied to the clipboard.")

    def _go_back(self) -> None:
        if self._service is not None:
            self._stop_host()
        self.on_back()
