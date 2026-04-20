@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Vytvorit_zastupce_s_ikonou.ps1"
pause
