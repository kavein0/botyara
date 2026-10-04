@echo off
setlocal
cd /d "%~dp0"
if not exist "clients\dotnet\publish\BotyaraClient.exe" (
    echo .NET client is not published. See clients\README.md.
    pause
    exit /b 1
)
"clients\dotnet\publish\BotyaraClient.exe"
if errorlevel 1 pause
