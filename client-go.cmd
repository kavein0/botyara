@echo off
setlocal
cd /d "%~dp0"
if not exist "clients\go\build\botyara-go.exe" (
    echo Go client is not built. See clients\README.md.
    pause
    exit /b 1
)
"clients\go\build\botyara-go.exe"
if errorlevel 1 pause
