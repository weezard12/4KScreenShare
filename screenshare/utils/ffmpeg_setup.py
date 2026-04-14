from __future__ import annotations

import os
import platform
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from screenshare.utils.process import hidden_subprocess_kwargs


FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"
WINDOWS_FFMPEG_PACKAGE_IDS = (
    "Gyan.FFmpeg",
    "Gyan.FFmpeg.Essentials",
    "BtbN.FFmpeg.Shared",
)


@dataclass(slots=True)
class FfmpegStatus:
    system_ffmpeg_path: str | None
    bundled_ffmpeg_path: str | None
    runtime_ffmpeg_path: str | None

    @property
    def runtime_available(self) -> bool:
        return self.runtime_ffmpeg_path is not None

    @property
    def missing_from_path(self) -> bool:
        return self.system_ffmpeg_path is None


@dataclass(slots=True)
class FfmpegInstallPlan:
    command: tuple[str, ...] | None
    summary: str
    manual_hint: str
    docs_url: str = FFMPEG_DOWNLOAD_URL

    @property
    def auto_install_supported(self) -> bool:
        return self.command is not None


@dataclass(slots=True)
class FfmpegInstallResult:
    plan: FfmpegInstallPlan
    status: FfmpegStatus
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        text = "\n".join(part.strip() for part in (self.stdout, self.stderr) if part.strip())
        return text.strip()


def probe_ffmpeg_status() -> FfmpegStatus:
    system_ffmpeg = shutil.which("ffmpeg")
    bundled_ffmpeg = _detect_bundled_ffmpeg()
    runtime_ffmpeg = bundled_ffmpeg or system_ffmpeg
    return FfmpegStatus(
        system_ffmpeg_path=system_ffmpeg,
        bundled_ffmpeg_path=bundled_ffmpeg,
        runtime_ffmpeg_path=runtime_ffmpeg,
    )


def build_ffmpeg_install_plan() -> FfmpegInstallPlan:
    system = platform.system().lower()
    if system == "windows":
        return _build_windows_install_plan()
    if system == "darwin":
        return _build_macos_install_plan()
    if system == "linux":
        return _build_linux_install_plan()
    return FfmpegInstallPlan(
        command=None,
        summary="Automatic FFmpeg installation is not available on this platform.",
        manual_hint=f"Install FFmpeg manually from {FFMPEG_DOWNLOAD_URL}.",
    )


def should_prompt_for_ffmpeg_setup(status: FfmpegStatus, *, is_frozen: bool) -> bool:
    if not status.runtime_available:
        return True
    return not is_frozen and status.missing_from_path


def install_ffmpeg(plan: FfmpegInstallPlan) -> FfmpegInstallResult:
    if plan.command is None:
        raise RuntimeError("Automatic FFmpeg installation is not available on this machine.")

    completed = subprocess.run(
        list(plan.command),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    _refresh_process_path()
    return FfmpegInstallResult(
        plan=plan,
        status=probe_ffmpeg_status(),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def open_ffmpeg_download_page(url: str = FFMPEG_DOWNLOAD_URL) -> None:
    webbrowser.open(url)


def _detect_bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None

    if not ffmpeg_path:
        return None
    ffmpeg_file = Path(ffmpeg_path)
    if ffmpeg_file.exists():
        return str(ffmpeg_file)
    return None


def _build_windows_install_plan() -> FfmpegInstallPlan:
    winget = shutil.which("winget")
    if winget:
        package_id = _resolve_windows_ffmpeg_package_id(winget) or WINDOWS_FFMPEG_PACKAGE_IDS[0]
        return FfmpegInstallPlan(
            command=(
                winget,
                "install",
                "--id",
                package_id,
                "--exact",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ),
            summary="Install FFmpeg automatically with WinGet.",
            manual_hint=(
                "If WinGet installation fails, install FFmpeg manually from the official download page "
                "or from Gyan's Windows builds."
            ),
        )

    return FfmpegInstallPlan(
        command=None,
        summary="WinGet is not available, so the app cannot install FFmpeg automatically on Windows.",
        manual_hint=(
            "Install WinGet first, or download a current FFmpeg build from "
            "https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH."
        ),
    )


def _build_macos_install_plan() -> FfmpegInstallPlan:
    brew = shutil.which("brew")
    if brew:
        return FfmpegInstallPlan(
            command=(brew, "install", "ffmpeg"),
            summary="Install FFmpeg automatically with Homebrew.",
            manual_hint="If Homebrew installation fails, reinstall or update Homebrew and try again.",
        )

    return FfmpegInstallPlan(
        command=None,
        summary="Homebrew is not available, so the app cannot install FFmpeg automatically on macOS.",
        manual_hint="Install Homebrew first, then run `brew install ffmpeg`.",
    )


def _build_linux_install_plan() -> FfmpegInstallPlan:
    prefix = _linux_auth_prefix()
    if shutil.which("apt-get"):
        command = tuple(prefix + [shutil.which("apt-get") or "apt-get", "install", "-y", "ffmpeg"])
        return FfmpegInstallPlan(
            command=command,
            summary="Install FFmpeg automatically with apt.",
            manual_hint="If apt-based installation fails, run `sudo apt-get install -y ffmpeg` manually.",
        )
    if shutil.which("dnf"):
        command = tuple(prefix + [shutil.which("dnf") or "dnf", "install", "-y", "ffmpeg"])
        return FfmpegInstallPlan(
            command=command,
            summary="Install FFmpeg automatically with dnf.",
            manual_hint="If dnf-based installation fails, run `sudo dnf install -y ffmpeg` manually.",
        )
    if shutil.which("pacman"):
        command = tuple(prefix + [shutil.which("pacman") or "pacman", "-Sy", "--noconfirm", "ffmpeg"])
        return FfmpegInstallPlan(
            command=command,
            summary="Install FFmpeg automatically with pacman.",
            manual_hint="If pacman-based installation fails, run `sudo pacman -Sy --noconfirm ffmpeg` manually.",
        )

    return FfmpegInstallPlan(
        command=None,
        summary="No supported Linux package manager was detected for automatic FFmpeg installation.",
        manual_hint="Install FFmpeg with your distribution's package manager.",
    )


def _resolve_windows_ffmpeg_package_id(winget: str) -> str | None:
    for package_id in WINDOWS_FFMPEG_PACKAGE_IDS:
        try:
            result = subprocess.run(
                [
                    winget,
                    "search",
                    "--id",
                    package_id,
                    "--exact",
                    "--accept-source-agreements",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            continue
        if result.returncode == 0 and package_id.lower() in result.stdout.lower():
            return package_id
    return None


def _linux_auth_prefix() -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    pkexec = shutil.which("pkexec")
    if pkexec:
        return [pkexec]
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo]
    return []


def _refresh_process_path() -> None:
    system = platform.system().lower()
    if system == "windows":
        _refresh_windows_path()


def _refresh_windows_path() -> None:
    try:
        import winreg
    except Exception:
        return

    path_parts: list[str] = []
    registry_targets = (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"Environment",
        ),
    )

    for root, subkey in registry_targets:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if value:
            path_parts.append(os.path.expandvars(str(value)))

    if path_parts:
        os.environ["PATH"] = os.pathsep.join(path_parts)
