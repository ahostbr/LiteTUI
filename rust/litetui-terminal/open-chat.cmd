@echo off
setlocal
color 07
title LiteTUI Rust LIVE
cd /d "%~dp0"
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" (
    start "" "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" -w new --size 126,44 new-tab --title "LiteTUI Rust LIVE" --suppressApplicationTitle --colorScheme Campbell -d "%~dp0." pwsh.exe -NoLogo -NoProfile -NoExit -File "%~dp0open-chat.ps1"
) else (
    pwsh.exe -NoLogo -NoProfile -NoExit -File "%~dp0open-chat.ps1"
)
