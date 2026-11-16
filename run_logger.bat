@echo off
rem HA-130TR logger launcher (double-click). Options: run_logger.bat --port COM5 --unit N
chcp 65001 > nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run these first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe ha130tr_logger.py %*
echo.
pause
