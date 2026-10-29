@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" scripts\run_server.py %*
) else (
    call python scripts\run_server.py %*
)
if errorlevel 1 pause
