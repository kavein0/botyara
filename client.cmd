@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment .venv is missing. Follow README.md to install it.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -u "client.py"
if errorlevel 1 pause
