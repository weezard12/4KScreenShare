from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import tkinter.messagebox as messagebox
from typing import Any, Coroutine

import customtkinter as ctk

from screenshare.gui.launcher import LauncherView
from screenshare.utils.ffmpeg_setup import (
    FfmpegInstallPlan,
    FfmpegInstallResult,
    FfmpegStatus,
    build_ffmpeg_install_plan,
    install_ffmpeg,
    open_ffmpeg_download_page,
    probe_ffmpeg_status,
    should_prompt_for_ffmpeg_setup,
)


def _close_boot_splash() -> None:
    try:
        import pyi_splash  # type: ignore[import-not-found]
    except Exception:
        return

    try:
        pyi_splash.close()
    except Exception:
        pass


class AsyncRuntime:
    """Runs a dedicated asyncio event loop away from the GUI thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="screenshare-asyncio",
            daemon=True,
        )
        self._started = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def start(self) -> None:
        self._thread.start()
        self._started.wait(timeout=5)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        if self._loop.is_closed():
            self._thread.join(timeout=5)
            return

        async def _shutdown() -> None:
            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._loop.stop()

        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            try:
                future.result(timeout=10)
            except concurrent.futures.TimeoutError:
                if not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()
        self._loop.close()


class ScreenShareApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("dark-blue")

        self.title("4K Screen Share")
        self.geometry("1360x860")
        self.minsize(1180, 760)

        self.runtime = AsyncRuntime()
        self.runtime.start()

        self._active_view: ctk.CTkFrame | None = None
        self._ffmpeg_status: FfmpegStatus | None = None
        self._ffmpeg_setup_window: ctk.CTkToplevel | None = None

        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.bind_all("<Control-q>", lambda _event: self.quit_app())
        self.bind_all("<Command-q>", lambda _event: self.quit_app())

        self.show_launcher()
        self.after(50, _close_boot_splash)
        self.after(250, self._maybe_prompt_ffmpeg_setup)

    def show_launcher(self) -> None:
        self._swap_view(LauncherView(self, on_host=self.show_host, on_join=self.show_viewer))

    def show_host(self) -> None:
        try:
            from screenshare.gui.host_view import HostView

            view = HostView(
                self,
                runtime=self.runtime,
                on_back=self.show_launcher,
                show_error=self.show_error,
            )
        except Exception as exc:
            self.show_error("Unable to open host mode", str(exc))
            return
        self._swap_view(view)

    def show_viewer(self) -> None:
        try:
            from screenshare.gui.viewer_view import ViewerView

            view = ViewerView(
                self,
                runtime=self.runtime,
                on_back=self.show_launcher,
                show_error=self.show_error,
            )
        except Exception as exc:
            self.show_error("Unable to open viewer mode", str(exc))
            return
        self._swap_view(view)

    def show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self)

    def quit_app(self) -> None:
        if self._ffmpeg_setup_window is not None and self._ffmpeg_setup_window.winfo_exists():
            self._ffmpeg_setup_window.destroy()

        if self._active_view is not None and hasattr(self._active_view, "cleanup"):
            try:
                self._active_view.cleanup()
            except Exception:
                pass

        try:
            self.runtime.stop()
        except Exception:
            pass

        self.destroy()

    def _swap_view(self, view: ctk.CTkFrame) -> None:
        if self._active_view is not None:
            if hasattr(self._active_view, "cleanup"):
                self._active_view.cleanup()
            self._active_view.pack_forget()
            self._active_view.destroy()

        self._active_view = view
        self._active_view.pack(fill="both", expand=True)

    def _maybe_prompt_ffmpeg_setup(self) -> None:
        self._ffmpeg_status = probe_ffmpeg_status()
        if not should_prompt_for_ffmpeg_setup(self._ffmpeg_status, is_frozen=getattr(sys, "frozen", False)):
            return

        plan = build_ffmpeg_install_plan()
        if self._ffmpeg_status.runtime_available:
            title = "Optional FFmpeg Setup"
            message = (
                "FFmpeg was not found in your system PATH.\n\n"
                "This app can still stream because a bundled FFmpeg runtime is available, "
                "but installing system FFmpeg will fully set up the machine for external tools.\n\n"
                f"Bundled runtime:\n{self._ffmpeg_status.runtime_ffmpeg_path}\n\n"
                f"{plan.summary}\n\n"
                "Install FFmpeg now?"
            )
        else:
            title = "FFmpeg Setup Required"
            message = (
                "FFmpeg was not found in PATH and no bundled runtime is available.\n\n"
                "Video streaming needs FFmpeg to start. "
                f"{plan.summary}\n\n"
                "Install FFmpeg now?"
            )

        if plan.auto_install_supported:
            if messagebox.askyesno(title, message, parent=self):
                self._start_ffmpeg_install(plan)
            elif not self._ffmpeg_status.runtime_available:
                self.show_error("FFmpeg Required", "Streaming will remain unavailable until FFmpeg is installed.")
            return

        details = f"{message}\n\nAutomatic installation is unavailable.\n\n{plan.manual_hint}"
        if messagebox.askyesno(title, details + "\n\nOpen the FFmpeg download page now?", parent=self):
            open_ffmpeg_download_page(plan.docs_url)

    def _start_ffmpeg_install(self, plan: FfmpegInstallPlan) -> None:
        self._show_ffmpeg_setup_progress("Installing FFmpeg", "The app is setting up FFmpeg. This can take a minute.")
        worker = threading.Thread(
            target=self._ffmpeg_install_worker,
            args=(plan,),
            name="ffmpeg-installer",
            daemon=True,
        )
        worker.start()

    def _ffmpeg_install_worker(self, plan: FfmpegInstallPlan) -> None:
        try:
            result = install_ffmpeg(plan)
        except Exception as exc:
            try:
                self.after(0, lambda: self._finish_ffmpeg_install_error(plan, str(exc)))
            except Exception:
                pass
            return
        try:
            self.after(0, lambda: self._finish_ffmpeg_install(result))
        except Exception:
            pass

    def _finish_ffmpeg_install(self, result: FfmpegInstallResult) -> None:
        self._close_ffmpeg_setup_progress()
        self._ffmpeg_status = result.status

        if result.succeeded and result.status.system_ffmpeg_path:
            messagebox.showinfo(
                "FFmpeg Installed",
                (
                    "FFmpeg was installed successfully.\n\n"
                    f"Detected path:\n{result.status.system_ffmpeg_path}\n\n"
                    "Restarting other terminals or apps may still be required before they see the updated PATH."
                ),
                parent=self,
            )
            return

        if result.succeeded:
            messagebox.showinfo(
                "FFmpeg Installed",
                (
                    "FFmpeg installation finished, but the current app session does not see the updated PATH yet.\n\n"
                    "Restart the app to pick up the new FFmpeg installation."
                ),
                parent=self,
            )
            return

        output = result.output or "No installer output was returned."
        trimmed_output = output[-1200:]
        message = (
            "FFmpeg installation failed.\n\n"
            f"{result.plan.manual_hint}\n\n"
            f"Installer output:\n{trimmed_output}"
        )
        if messagebox.askyesno("FFmpeg Installation Failed", message + "\n\nOpen the FFmpeg download page now?", parent=self):
            open_ffmpeg_download_page(result.plan.docs_url)

    def _finish_ffmpeg_install_error(self, plan: FfmpegInstallPlan, error_text: str) -> None:
        self._close_ffmpeg_setup_progress()
        if messagebox.askyesno(
            "FFmpeg Installation Failed",
            (
                "The app could not start the FFmpeg installer.\n\n"
                f"{error_text}\n\n"
                f"{plan.manual_hint}\n\n"
                "Open the FFmpeg download page now?"
            ),
            parent=self,
        ):
            open_ffmpeg_download_page(plan.docs_url)

    def _show_ffmpeg_setup_progress(self, title: str, message: str) -> None:
        if self._ffmpeg_setup_window is not None and self._ffmpeg_setup_window.winfo_exists():
            self._ffmpeg_setup_window.destroy()

        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("440x170")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)

        ctk.CTkLabel(
            dialog,
            text=title,
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(anchor="w", padx=24, pady=(20, 8))

        ctk.CTkLabel(
            dialog,
            text=message,
            justify="left",
            wraplength=388,
            text_color="#c7d5e0",
        ).pack(fill="x", padx=24)

        progress = ctk.CTkProgressBar(dialog, mode="indeterminate")
        progress.pack(fill="x", padx=24, pady=(18, 20))
        progress.start()

        self._ffmpeg_setup_window = dialog

    def _close_ffmpeg_setup_progress(self) -> None:
        if self._ffmpeg_setup_window is not None and self._ffmpeg_setup_window.winfo_exists():
            self._ffmpeg_setup_window.grab_release()
            self._ffmpeg_setup_window.destroy()
        self._ffmpeg_setup_window = None


def main() -> None:
    app = ScreenShareApp()
    app.mainloop()


if __name__ == "__main__":
    main()
