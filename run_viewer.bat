@echo off
rem HA-130TR data viewer (double-click). Optional: run_viewer.bat D:\other\folder
rem Python: Anaconda base if present, otherwise .venv.
cd /d "%~dp0"
set "PYW="
if not defined PYW if exist "%ProgramData%\anaconda3\pythonw.exe" set "PYW=%ProgramData%\anaconda3\pythonw.exe"
if not defined PYW if exist "%USERPROFILE%\anaconda3\pythonw.exe" set "PYW=%USERPROFILE%\anaconda3\pythonw.exe"
if not defined PYW if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"
if not defined PYW (
    echo [ERROR] Python not found. Install Anaconda, or create .venv:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" "%PYW%" data_viewer.py %*
