@echo off
title JobHunter - LIVE log do konce behu
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist "tools\night_logs\run_*.log" (
    echo.
    echo Zadny run_*.log zatim neexistuje. Spust AUTO.bat v root slozce.
    echo.
    pause
    exit /b 1
)

echo Tail logu dokud se v souboru neobjevi konec behu ^(### JOBHUNTER_*_END^).
python -u "tools\watch_tail.py" --until-end
echo.
echo Okno muzes zavrit.
pause
