@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Galgame2Voice Launcher
echo ========================================================
echo       Galgame2Voice - GPT-SoVITS Companion Service
echo ========================================================
echo.

:: 1. Determine Project Root Directory
set "SCRIPT_DIR=%~dp0"
pushd "%~dp0.."
set "PROJECT_ROOT=!CD!"
popd
cd /d "%PROJECT_ROOT%"

:: 2. Detect Python Environment
set "PYTHON_EXE="
if defined PYTHON_BIN set "PYTHON_EXE=%PYTHON_BIN%"
if defined PYTHON_CMD set "PYTHON_EXE=%PYTHON_CMD%"
if not defined PYTHON_EXE if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_ROOT%\venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
if not defined PYTHON_EXE (
    where python >nul 2>nul
    if !errorlevel! equ 0 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
    where py >nul 2>nul
    if !errorlevel! equ 0 (
        for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
        if not defined PYTHON_EXE set "PYTHON_EXE=py"
    )
)
if not defined PYTHON_EXE (
    echo [ERROR] Python 3.10+ was not found on your system PATH!
    echo Please install Python 3.10+ from https://www.python.org/ or add it to PATH.
    pause
    endlocal
    exit /b 1
)

:: 3. Prepare Directories & Install Dependencies
echo [1/5] Checking runtime directories and dependencies...
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "audio" mkdir audio

if not "%GALGAME_SKIP_PIP%"=="1" (
    where uv >nul 2>nul
    if !errorlevel! equ 0 (
        uv pip install --system -q -r requirements.txt >nul 2>&1 || uv pip install -q -r requirements.txt >nul 2>&1
    ) else (
        "%PYTHON_EXE%" -m pip install -q -r requirements.txt >nul 2>&1
    )
)

:: 4. Port & Process Conflict Check
set "APP_PORT=8080"
if defined GALGAME_PORT set "APP_PORT=%GALGAME_PORT%"
if defined APP_PORT_OVERRIDE set "APP_PORT=%APP_PORT_OVERRIDE%"

if exist "galgame2voice.pid" (
    set /p EXISTING_PID=<galgame2voice.pid
    set "EXISTING_PID=!EXISTING_PID: =!"
    if defined EXISTING_PID (
        tasklist /fi "PID eq !EXISTING_PID!" 2>nul | findstr /i "!EXISTING_PID!" >nul
        if !errorlevel! equ 0 (
            echo [WARN] Galgame2Voice is already running [PID: !EXISTING_PID!].
            echo Opening browser...
            start http://127.0.0.1:%APP_PORT%/
            endlocal
            exit /b 0
        ) else (
            del /f /q galgame2voice.pid >nul 2>&1
        )
    )
)

:: Check if port is occupied by another process
set "OCCUPIED_PID="
for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":%APP_PORT%"') do (
    if "%%a"=="LISTENING" set "OCCUPIED_PID=%%b"
)
if defined OCCUPIED_PID (
    echo [ERROR] Port %APP_PORT% is already in use by process PID !OCCUPIED_PID!.
    echo Please terminate that process or set GALGAME_PORT to another port.
    pause
    endlocal
    exit /b 1
)

:: 5. GPT-SoVITS Backend Auto-Detection & Launch
set "SOVITS_URL=http://127.0.0.1:9880"
if defined GPT_SOVITS_URL set "SOVITS_URL=%GPT_SOVITS_URL%"
echo [2/5] Checking GPT-SoVITS service at %SOVITS_URL%...
curl -s -m 2 "%SOVITS_URL%/" >nul 2>&1
if !errorlevel! equ 0 (
    echo [OK] GPT-SoVITS backend detected and responding.
) else (
    echo [..] GPT-SoVITS not running on port 9880, probing local paths...
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
    if not defined SOVITS_DIR if exist "%PROJECT_ROOT%\..\GPT-SoVITS\api_v2.py" set "SOVITS_DIR=%PROJECT_ROOT%\..\GPT-SoVITS"

    if defined SOVITS_DIR (
        pushd "!SOVITS_DIR!"
        set "SOVITS_PY=.\runtime\python.exe"
        if not exist "!SOVITS_PY!" set "SOVITS_PY=python"
        start "GPT-SoVITS-Engine" /min !SOVITS_PY! api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
        popd
        echo [OK] Automatically launched GPT-SoVITS in background.
    ) else (
        echo [INFO] GPT-SoVITS backend not detected at %SOVITS_URL%.
    )
)

:: 6. Launch Service in Background with Uvicorn
echo [3/5] Starting Galgame2Voice uvicorn server on port %APP_PORT%...
start "Galgame2Voice Service" /min "%PYTHON_EXE%" -m uvicorn galgame2voice.main:app --host 127.0.0.1 --port %APP_PORT%

:: 7. Wait for Service Startup & Capture PID
echo [4/5] Waiting for service startup...
set "ATTEMPTS=0"
set "NEW_PID="

:HEALTH_LOOP
timeout /t 1 /nobreak >nul
set /a ATTEMPTS+=1

for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":%APP_PORT%"') do (
    if "%%a"=="LISTENING" set "NEW_PID=%%b"
)

if defined NEW_PID (
    echo !NEW_PID! > galgame2voice.pid
    goto CHECK_HEALTH
)

if !ATTEMPTS! lss 15 goto HEALTH_LOOP

:CHECK_HEALTH
curl -s -m 2 "http://127.0.0.1:%APP_PORT%/api/health" >nul 2>&1
if !errorlevel! equ 0 (
    echo [5/5] [SUCCESS] Galgame2Voice is online at http://127.0.0.1:%APP_PORT%/
    if defined NEW_PID echo       Service PID: !NEW_PID!
    start http://127.0.0.1:%APP_PORT%/
) else (
    echo [WARN] Service started but health check timed out.
    echo Please inspect logs/galgame2voice.log for details.
)

echo.
echo Use stop-galgame2voice.bat to shut down the service.
endlocal
exit /b 0
