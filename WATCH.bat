@echo off
chcp 65001 > nul
title JobHunter - LIVE log
cd /d "%~dp0tools\night_logs"

echo.
echo ==============================================================
echo   JOB HUNTER - LIVE log viewer    Made by Wux with ^<3
echo ==============================================================
echo.
echo Sleduju nejnovejsi run log v realnem case.
echo Zavreni okna tohle sledovani zastavi (smycka bezi nezavisle).
echo.

if not exist run_*.log (
    echo Zadny run_*.log zatim neexistuje. Spust AUTO.bat v root slozce.
    pause
    exit /b
)

powershell -NoProfile -Command "Get-ChildItem 'run_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | ForEach-Object { Write-Host \"=== Sleduji: $($_.Name) ===\" -ForegroundColor Cyan; Get-Content $_.FullName -Wait -Tail 80 }"

pause
