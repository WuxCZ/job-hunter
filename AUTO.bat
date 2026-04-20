@echo off
chcp 65001 > nul
title JobHunter AUTO mode
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo.
echo ==============================================================
echo   JOB HUNTER - AUTO MODE
echo   Made by Wux with ^<3
echo ==============================================================
echo.
echo  - Opakovane pousti Safe-mode beh (min fit 50, rate-limit 20s)
echo  - Max 30 odeslani na jeden beh, mezi behy 40 minut pauza
echo  - Automaticky stop v 08:00 rano (prepsatelne v tools\night_loop.py)
echo  - Windows sleep zamcen, pocitac neusne
echo  - Kazdy beh ma svuj log v tools\night_logs\run_XX_*.log
echo.
echo VSE ZUSTAVA V TOMTO OKNE. Zavrenim okna smycka konci.
echo Pro cisty stop stiskni Ctrl+C.
echo.

python -u tools\night_loop.py

echo.
echo ============  AUTO mode skoncil. Made by Wux with ^<3  ============
pause
