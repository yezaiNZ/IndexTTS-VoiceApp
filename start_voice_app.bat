@echo off
setlocal
cd /d "%~dp0"
set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%APP_PYTHON%" (
  echo Voice App is not installed. Run setup_voice_app.bat first.
  if /i not "%~1"=="--check-only" pause
  exit /b 1
)
"%APP_PYTHON%" -m voice_app.source_runtime --repair
if errorlevel 1 (
  echo Qt startup check failed. No model files were changed.
  if /i not "%~1"=="--check-only" pause
  exit /b 1
)
if /i "%~1"=="--check-only" exit /b 0
"%APP_PYTHON%" -m voice_app desktop
set "VOICE_APP_EXIT=%ERRORLEVEL%"
if not "%VOICE_APP_EXIT%"=="0" pause
exit /b %VOICE_APP_EXIT%
