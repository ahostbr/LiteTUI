@echo off
setlocal
color 07
title LiteTUI Rust Preview
cd /d "%~dp0"
if not exist "target\debug\litetui-rs.exe" (
    where cargo >nul 2>nul
    if errorlevel 1 (
        echo Preview binary is missing. Install Rust, then run cargo build --locked in this folder.
        pause
        exit /b 1
    )
    cargo build --locked
    if errorlevel 1 (
        echo Preview build failed. No runtime or tools were started.
        pause
        exit /b 1
    )
)
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" (
    start "" "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" -w new --size 126,44 new-tab --title "LiteTUI Rust Preview" --suppressApplicationTitle --colorScheme Campbell -d "%~dp0." "%~dp0target\debug\litetui-rs.exe" --preview
) else (
    "target\debug\litetui-rs.exe" --preview
)
