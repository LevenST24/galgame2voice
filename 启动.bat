@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

echo 正在启动 Galgame2Voice...

set "PYTHON_EXE=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

"%PYTHON_EXE%" scripts\run_server.py %*
if %errorlevel% neq 0 (
    echo.
    echo 服务已退出。
    pause
)
