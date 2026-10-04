@echo off
setlocal
cd /d "%~dp0"
if not exist "clients\cpp\build\botyara-cpp.exe" (
    echo C++ client is not built. See clients\README.md.
    pause
    exit /b 1
)
"clients\cpp\build\botyara-cpp.exe"
if errorlevel 1 pause
