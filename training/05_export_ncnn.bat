@echo off
setlocal
cd /d "%~dp0.."
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
".venv\Scripts\python.exe" "training\export_ncnn.py" --target all
pause
