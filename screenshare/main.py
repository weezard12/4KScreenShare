from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import tkinter.messagebox as messagebox
from typing import Any, Coroutine

import customtkinter as ctk

from screenshare.gui.host_view import HostView
from screenshare.gui.launcher import LauncherView
from screenshare.gui.viewer_view import ViewerView


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

        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.bind_all("<Control-q>", lambda _event: self.quit_app())
        self.bind_all("<Command-q>", lambda _event: self.quit_app())

        self.show_launcher()

    def show_launcher(self) -> None:
        self._swap_view(LauncherView(self, on_host=self.show_host, on_join=self.show_viewer))

    def show_host(self) -> None:
        try:
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


def main() -> None:
    app = ScreenShareApp()
    app.mainloop()


if __name__ == "__main__":
    main()
