@echo off
setlocal
title botyara - host
set "BOT_PROFILE=host"
set "BOT_CONFIG="
call "%~dp0client.cmd"
pause