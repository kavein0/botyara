@echo off
setlocal
cd /d "%~dp0"
if not exist "clients\rust\target\release\botyara-rust.exe" (
    echo Rust client is not built. See clients\README.md.
    pause
    exit /b 1
)
"clients\rust\target\release\botyara-rust.exe"
if errorlevel 1 pause
