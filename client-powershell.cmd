@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -File "clients\powershell\client.ps1"
if errorlevel 1 pause
