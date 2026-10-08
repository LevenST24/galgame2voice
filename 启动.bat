@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

python scripts\run_server.py %*
if errorlevel 1 pause
