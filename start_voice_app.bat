@echo off
cd /d "%~dp0"
set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%APP_PYTHON%" (
  echo Voice App is not installed. Run setup_voice_app.bat first.
  pause
  exit /b 1
)
"%APP_PYTHON%" -m voice_app desktop
if errorlevel 1 pause
