@echo off
setlocal
cd /d "%~dp0.."
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Training environment .venv was not found.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "training\check_environment.py"
pause
