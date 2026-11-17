@echo off
rem HA-130TR logger launcher (double-click). Options: run_logger.bat --port COM5 --unit N
rem Python: Anaconda base if present, otherwise .venv.
chcp 65001 > nul
cd /d "%~dp0"
set "PY="
if not defined PY if exist "%ProgramData%\anaconda3\python.exe" set "PY=%ProgramData%\anaconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    echo [ERROR] Python not found. Install Anaconda, or create .venv:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
set PYTHONIOENCODING=utf-8
"%PY%" ha130tr_logger.py %*
echo.
pause
