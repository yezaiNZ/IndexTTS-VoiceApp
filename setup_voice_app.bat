@echo off
setlocal
cd /d "%~dp0"
set "UV_CACHE_DIR=%~dp0.uv-cache"
where uv >nul 2>&1
if errorlevel 1 (
  echo uv was not found. Install uv first: https://docs.astral.sh/uv/getting-started/installation/
  if /i not "%~1"=="--no-pause" pause
  exit /b 1
)
uv sync --locked --extra desktop --python 3.11
if errorlevel 1 (
  if /i not "%~1"=="--no-pause" pause
  exit /b 1
)
rem Removing the old PySide6 meta package can remove files shared with Essentials.
rem Validate actual DLL imports after sync, not only the package inventory.
"%~dp0.venv\Scripts\python.exe" -m voice_app.source_runtime --repair
if errorlevel 1 (
  echo Qt startup check failed. No model files were changed.
  if /i not "%~1"=="--no-pause" pause
  exit /b 1
)
echo Voice App desktop environment is ready.
echo Start the app and select your existing IndexTTS directory in the setup window.
if /i not "%~1"=="--no-pause" pause
exit /b 0
