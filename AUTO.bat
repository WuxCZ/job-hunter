@echo off
chcp 65001 > nul
title JobHunter AUTO mode (watchdog)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo.
echo ==============================================================
echo   JOB HUNTER - AUTO MODE (watchdog)
echo   Made by Wux with ^<3
echo ==============================================================
echo.
echo  - Watchdog kazdych 5 min kontroluje, ze night_loop.py bezi.
echo  - Kdyz padne nebo zatuhne (log > 15 min beze zmeny), restartuje.
echo  - Night_loop pousti `python main.py run` opakovane:
echo      max 30 odeslani, min fit 50, pauza 20s, stop v 08:00.
echo  - Windows sleep je zamceny - pocitac neusne.
echo  - Kazdy beh ma svuj log v tools\night_logs\run_XX_*.log
echo  - Watchdog ma vlastni log: tools\night_logs\watchdog.log
echo.
echo ZAVRENIM TOHOTO OKNA PRESTANE WATCHDOG RESTARTOVAT (smycka jede dal az do konce aktualni iterace).
echo Pro uplny stop: Ctrl+C, pak zavri zbytek oken.
echo.

python -u tools\watchdog.py

echo.
echo ============  AUTO mode skoncil. Made by Wux with ^<3  ============
pause
