@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Stop Galgame2Voice
echo ========================================================
echo        [INFO] Stopping Galgame2Voice and GPT-SoVITS
echo ========================================================
echo.

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "SCRIPT_DIR=%~dp0"

REM 1. Terminate Galgame2Voice Port 8080 and active port
echo [1/2] Closing Galgame2Voice [Port 8080]
for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":8080"') do (
    if "%%a"=="LISTENING" (
        taskkill /f /t /pid %%b >nul 2>&1
        echo   [OK] Closed port 8080 process PID: %%b
    )
)
if exist "data\active_port.txt" (
    set /p DYN_PORT=<data\active_port.txt
    set "DYN_PORT=!DYN_PORT: =!"
    if defined DYN_PORT if not "!DYN_PORT!"=="8080" (
        for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":!DYN_PORT!"') do (
            if "%%a"=="LISTENING" taskkill /f /t /pid %%b >nul 2>&1
        )
    )
    del /f /q "data\active_port.txt" >nul 2>&1
)
if exist "galgame2voice.pid" (
    set /p SAVED_PID=<galgame2voice.pid
    set "SAVED_PID=!SAVED_PID: =!"
    if defined SAVED_PID taskkill /f /t /pid !SAVED_PID! >nul 2>&1
    del /f /q galgame2voice.pid >nul 2>&1
)

REM 2. Terminate GPT-SoVITS Port 9880
echo [2/2] Closing GPT-SoVITS [Port 9880]
for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":9880"') do (
    if "%%a"=="LISTENING" (
        taskkill /f /t /pid %%b >nul 2>&1
        echo   [OK] Closed port 9880 process PID: %%b
    )
)
if exist "gptsovits.pid" (
    set /p SOVITS_PID=<gptsovits.pid
    set "SOVITS_PID=!SOVITS_PID: =!"
    if defined SOVITS_PID taskkill /f /t /pid !SOVITS_PID! >nul 2>&1
    del /f /q gptsovits.pid >nul 2>&1
)
if exist "gpt_sovits.pid" (
    set /p SOVITS_PID=<gpt_sovits.pid
    set "SOVITS_PID=!SOVITS_PID: =!"
    if defined SOVITS_PID taskkill /f /t /pid !SOVITS_PID! >nul 2>&1
    del /f /q gpt_sovits.pid >nul 2>&1
)

echo.
echo ========================================================
echo   [OK] All services stopped, ports and VRAM released!
echo ========================================================
echo.

endlocal
exit /b 0
