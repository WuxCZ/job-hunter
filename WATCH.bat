@echo off
title JobHunter - LIVE log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist "tools\night_logs\run_*.log" (
    echo.
    echo Zadny run_*.log zatim neexistuje. Spust AUTO.bat v root slozce.
    echo.
    pause
    exit /b 1
)

python -u "tools\watch_tail.py"
if errorlevel 1 pause
