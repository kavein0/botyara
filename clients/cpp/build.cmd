@echo off
setlocal
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT (echo Visual Studio Build Tools not found & exit /b 1)
call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cd /d "%~dp0"
if not exist build mkdir build
cl /nologo /EHsc /std:c++17 /O2 /MT /utf-8 /I. /Fo:build\ /Fe:build\botyara-cpp.exe main.cpp
