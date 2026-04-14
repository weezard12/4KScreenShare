$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    uv venv .venv-build --python 3.12 --cache-dir .uv-cache
}

uv pip install --python .venv-build\Scripts\python.exe --cache-dir .uv-cache -r requirements.txt pyinstaller

& .\.venv-build\Scripts\pyinstaller.exe --clean --noconfirm screenshare.spec

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $root\dist\4KScreenShare.exe"
