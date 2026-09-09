@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
setlocal enabledelayedexpansion
set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" "%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1 && set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PY_EXE python -c "import sys" >nul 2>&1 && set "PY_EXE=python"
if not defined PY_EXE py -c "import sys" >nul 2>&1 && set "PY_EXE=py"
if not defined PY_EXE (echo [错误] 未检测到 Python 环境，请安装 Python 3.10+ & pause & endlocal & exit /b 1)
"%PY_EXE%" scripts\run_server.py %*
if errorlevel 1 pause
endlocal
