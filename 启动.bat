@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Galgame2Voice Launcher
echo ========================================================
echo       [Galgame2Voice] Smart AI Voice Companion Launcher
echo ========================================================
echo.

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "SCRIPT_DIR=%~dp0"

REM 1. Detect Python Environment
set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if !errorlevel! neq 0 (
        echo [ERROR] Python not found on system!
        pause
        endlocal
        exit /b 1
    )
    set "PYTHON_EXE=python"
)

echo [OK] Python detected: %PYTHON_EXE%

REM 2. Runtime Directories
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "audio" mkdir audio

set "APP_PORT=8080"
if defined GALGAME_PORT set "APP_PORT=%GALGAME_PORT%"

REM 3. Check Port 8080 Collision with netstat
set "OCCUPIED_PID="
for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":%APP_PORT% "') do (
    if "%%a"=="LISTENING" set "OCCUPIED_PID=%%b"
)

REM 4. Multi-drive scanning for GPT-SoVITS
set "SOVITS_DIR="
if defined GPT_SOVITS_DIR if exist "%GPT_SOVITS_DIR%\api_v2.py" set "SOVITS_DIR=%GPT_SOVITS_DIR%"
if not defined SOVITS_DIR if exist "E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604\api_v2.py" set "SOVITS_DIR=E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604"
if not defined SOVITS_DIR if exist "E:\GPT-SoVITS-v2pro-20250604\api_v2.py" set "SOVITS_DIR=E:\GPT-SoVITS-v2pro-20250604"
if not defined SOVITS_DIR if exist "D:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604\api_v2.py" set "SOVITS_DIR=D:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604"
if not defined SOVITS_DIR if exist "D:\GPT-SoVITS-v2pro-20250604\api_v2.py" set "SOVITS_DIR=D:\GPT-SoVITS-v2pro-20250604"
if not defined SOVITS_DIR if exist "C:\GPT-SoVITS-v2pro-20250604\api_v2.py" set "SOVITS_DIR=C:\GPT-SoVITS-v2pro-20250604"
if not defined SOVITS_DIR if exist "C:\GPT-SoVITS\api_v2.py" set "SOVITS_DIR=C:\GPT-SoVITS"
if not defined SOVITS_DIR if exist "D:\GPT-SoVITS\api_v2.py" set "SOVITS_DIR=D:\GPT-SoVITS"
if not defined SOVITS_DIR if exist "E:\GPT-SoVITS\api_v2.py" set "SOVITS_DIR=E:\GPT-SoVITS"
if not defined SOVITS_DIR if exist "%~dp0..\GPT-SoVITS\api_v2.py" set "SOVITS_DIR=%~dp0..\GPT-SoVITS"

set "SOVITS_PY=.\runtime\python.exe"

REM 5. Bounded health polling loop reference
REM for /l %%i in (1,1,15) do ( timeout /t 1 /nobreak >nul & curl -s -m 2 http://127.0.0.1:8080/api/health )
REM Reference: uvicorn galgame2voice.main:app --host 127.0.0.1 --port 8080

"%PYTHON_EXE%" scripts\run_server.py %*
if errorlevel 1 (
    echo [ERROR] Service launch failed.
    pause
)

endlocal
exit /b 0
