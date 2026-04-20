@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JobHunter
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py gui
) else (
    python main.py gui
)
if errorlevel 1 (
    echo.
    echo Spusteni selhalo. Zkontroluj Python a zavislosti: pip install -r requirements.txt
    pause
)
