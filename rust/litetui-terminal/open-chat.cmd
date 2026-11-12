@echo off
setlocal
color 07
title LiteTUI Rust LIVE
cd /d "%~dp0"
REM One window: run in the console this was started from (a double-click, run-rust.bat or
REM start). Opening a new Windows Terminal window from here left the starting console
REM behind as a second, blank window (Ryan 2026-11-12).
pwsh.exe -NoLogo -NoProfile -NoExit -File "%~dp0open-chat.ps1"
