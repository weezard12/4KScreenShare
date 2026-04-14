from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all


project_root = Path.cwd()
python_home = Path(sys.base_prefix)

datas = []
binaries = []
hiddenimports = []

for package_name in (
    "tkinter",
    "customtkinter",
    "darkdetect",
    "av",
    "aiohttp",
    "aiortc",
    "PIL",
    "mss",
    "sounddevice",
    "numpy",
    "pyautogui",
    "imageio_ffmpeg",
):
    collected_datas, collected_binaries, collected_hiddenimports = collect_all(package_name)
    datas += collected_datas
    binaries += collected_binaries
    hiddenimports += collected_hiddenimports

hiddenimports += [
    "tkinter",
    "tkinter.constants",
    "tkinter.filedialog",
    "tkinter.font",
    "tkinter.ttk",
    "tkinter.messagebox",
    "PIL._tkinter_finder",
]

for binary_name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
    binary_path = python_home / "DLLs" / binary_name
    if binary_path.exists():
        binaries.append((str(binary_path), "."))

tk_data_layout = (
    ("tcl8.6", "_tcl_data"),
    ("tk8.6", "_tk_data"),
    ("tcl8", "tcl8"),
)

for source_name, bundle_name in tk_data_layout:
    source_dir = python_home / "tcl" / source_name
    if source_dir.exists():
        for source_file in source_dir.rglob("*"):
            if source_file.is_file():
                relative_parent = source_file.relative_to(source_dir).parent
                target_dir = Path(bundle_name) / relative_parent
                datas.append((str(source_file), str(target_dir)))

a = Analysis(
    ["screenshare/main.py"],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[str(project_root / "pyinstaller_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="4KScreenShare",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=True,
)
