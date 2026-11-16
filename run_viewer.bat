@echo off
rem HA-130TR data viewer (double-click). Optional: run_viewer.bat D:\other\folder
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] .venv not found. Run these first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" .venv\Scripts\pythonw.exe data_viewer.py %*
