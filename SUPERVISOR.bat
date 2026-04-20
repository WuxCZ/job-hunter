@echo off
chcp 65001 > nul
title JobHunter - SUPERVISOR (watchdog only)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo.
echo ==============================================================
echo   JOB HUNTER - SUPERVISOR
echo   Made by Wux with ^<3
echo ==============================================================
echo.
echo  Watchdog puze - predpoklada, ze night_loop uz bezi.
echo  Kazdych 5 min check, v pripade padu restartuje.
echo  Zavrenim okna se watchdog zastavi (ale smycka jede dal).
echo.

python -u tools\watchdog.py

echo.
echo ============  Supervisor skoncil. ============
pause
