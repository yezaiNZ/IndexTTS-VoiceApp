@echo off
cd /d "%~dp0"
set "UV_CACHE_DIR=%~dp0.uv-cache"
where uv >nul 2>&1
if errorlevel 1 (
  echo uv was not found. Install uv first: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv sync --locked --extra desktop --python 3.11
if errorlevel 1 (
  pause
  exit /b 1
)
echo Voice App desktop environment is ready.
echo Start the app and select your existing IndexTTS directory in the setup window.
pause
