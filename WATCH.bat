@echo off
chcp 65001 > nul
title JobHunter - LIVE log
cd /d "%~dp0tools\night_logs"

echo.
echo ==============================================================
echo   JOB HUNTER - LIVE log viewer    Made by Wux with ^<3
echo ==============================================================
echo.
echo Sleduju nejnovejsi run log + watchdog heartbeaty.
echo Zavreni okna tohle sledovani zastavi (smycka bezi nezavisle).
echo.

if not exist run_*.log (
    echo Zadny run_*.log zatim neexistuje. Spust AUTO.bat v root slozce.
    pause
    exit /b
)

powershell -NoProfile -Command "$run = Get-ChildItem 'run_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1; $wdPath = 'watchdog.log'; Write-Host \"=== Sleduji run: $($run.Name) ===\" -ForegroundColor Cyan; if (Test-Path $wdPath) { Write-Host \"=== + watchdog heartbeats ===\" -ForegroundColor Yellow }; Get-Content $run.FullName -Wait -Tail 80"

pause
