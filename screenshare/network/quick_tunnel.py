from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Callable

from screenshare.utils.process import hidden_subprocess_kwargs


StatusCallback = Callable[[str], None]

_QUICK_TUNNEL_PATTERN = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com", re.IGNORECASE)
_WINDOWS_DOWNLOAD_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-windows-amd64.exe"
)


class QuickTunnelError(RuntimeError):
    """Raised when a public Quick Tunnel could not be prepared."""


def probe_cloudflared_binary() -> str | None:
    from_path = shutil.which("cloudflared")
    if from_path:
        return from_path

    binary_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        bundled = Path(frozen_root) / binary_name
        if bundled.exists():
            return str(bundled)

    cached = _cache_dir() / binary_name
    if cached.exists():
        return str(cached)
    return None


def ensure_cloudflared_binary(*, on_status: StatusCallback | None = None) -> str:
    existing = probe_cloudflared_binary()
    if existing:
        return existing

    if platform.system().lower() != "windows":
        raise QuickTunnelError(
            "cloudflared is not installed. Install Cloudflare Tunnel to enable automatic internet sessions."
        )

    target_dir = _cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "cloudflared.exe"
    temp_path = target_path.with_suffix(".download")
    if on_status is not None:
        on_status("Downloading Cloudflare Tunnel support for internet sessions...")
    try:
        with urllib.request.urlopen(_WINDOWS_DOWNLOAD_URL, timeout=60) as response, temp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        os.replace(temp_path, target_path)
    except Exception as exc:
        with suppress(Exception):
            temp_path.unlink(missing_ok=True)
        raise QuickTunnelError(
            "The app could not download cloudflared automatically. "
            "Install Cloudflare Tunnel or check internet access and try again."
        ) from exc
    return str(target_path)


class QuickTunnelProcess:
    def __init__(self, *, on_status: StatusCallback | None = None) -> None:
        self.on_status = on_status
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._url_ready = threading.Event()
        self._url: str | None = None
        self._error: str | None = None
        self._isolated_home: tempfile.TemporaryDirectory[str] | None = None
        self._lock = threading.Lock()

    @property
    def public_url(self) -> str | None:
        return self._url

    def start(self, local_url: str, *, timeout: float = 35.0) -> str:
        last_error: str | None = None
        for attempt in range(1, 4):
            try:
                return self._start_once(local_url, timeout=timeout)
            except QuickTunnelError as exc:
                last_error = str(exc)
                self.stop()
                if attempt < 3:
                    self._emit_status(f"Retrying automatic internet relay setup ({attempt + 1}/3)...")
        raise QuickTunnelError(last_error or "The public tunnel could not be prepared.")

    def _start_once(self, local_url: str, *, timeout: float) -> str:
        started_at = time.perf_counter()
        with self._lock:
            if self._process is not None and self._url:
                return self._url
            binary = ensure_cloudflared_binary(on_status=self._emit_status)
            self._url_ready.clear()
            self._url = None
            self._error = None
            self._isolated_home = tempfile.TemporaryDirectory(prefix="screenshare-cloudflared-")
            home_path = self._isolated_home.name
            command = [
                binary,
                "tunnel",
                "--url",
                local_url,
                "--no-autoupdate",
            ]
            env = os.environ.copy()
            env["HOME"] = home_path
            env["XDG_CONFIG_HOME"] = home_path
            if os.name == "nt":
                env["USERPROFILE"] = home_path
                env["APPDATA"] = home_path
                env["LOCALAPPDATA"] = home_path

            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **hidden_subprocess_kwargs(),
            )
            self._reader_thread = threading.Thread(
                target=self._read_output,
                name="cloudflared-quick-tunnel",
                daemon=True,
            )
            self._reader_thread.start()

        if not self._url_ready.wait(timeout=timeout):
            self.stop()
            detail = self._error or "The public tunnel did not become ready in time."
            raise QuickTunnelError(detail)
        if not self._url:
            self.stop()
            raise QuickTunnelError(self._error or "The public tunnel did not return a public URL.")
        elapsed = time.perf_counter() - started_at
        remaining = max(5.0, timeout - elapsed)
        self._wait_until_reachable(remaining)
        self._emit_status("Internet join code is ready through an outbound public tunnel.")
        return self._url

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is not None:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                process.wait(timeout=2)
        current = threading.current_thread()
        if self._reader_thread is not None and self._reader_thread.is_alive() and self._reader_thread is not current:
            self._reader_thread.join(timeout=2)
        self._reader_thread = None
        if self._isolated_home is not None:
            self._isolated_home.cleanup()
            self._isolated_home = None
        self._url_ready.clear()
        self._url = None

    def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._error = "The public tunnel process did not start correctly."
            self._url_ready.set()
            return

        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                match = _QUICK_TUNNEL_PATTERN.search(line)
                if match:
                    self._url = match.group(0).rstrip("/")
                    self._url_ready.set()
                    continue
                lowered = line.lower()
                if "error" in lowered or "failed" in lowered:
                    self._error = line
        finally:
            returncode = process.poll()
            if self._url is None and self._error is None:
                self._error = f"cloudflared exited before publishing a tunnel URL (exit code {returncode})."
            self._url_ready.set()

    def _wait_until_reachable(self, timeout: float) -> None:
        if not self._url:
            raise QuickTunnelError("The public tunnel URL is unavailable.")

        health_url = f"{self._url}/health"
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=4) as response:
                    if int(getattr(response, "status", 0) or 0) >= 200:
                        return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1.0)
                continue

        raise QuickTunnelError(
            "The public tunnel URL was issued but never became reachable. "
            f"Last probe error: {last_error or 'unknown error'}"
        )

    def _emit_status(self, message: str) -> None:
        if self.on_status is not None:
            self.on_status(message)


def _cache_dir() -> Path:
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "4KScreenShare" / "tools"
