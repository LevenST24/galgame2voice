@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Stop Galgame2Voice
echo ========================================================
echo            Stopping Galgame2Voice Service
echo ========================================================
echo.

:: 1. Determine Project Root Directory
set "SCRIPT_DIR=%~dp0"
pushd "%~dp0.."
set "PROJECT_ROOT=!CD!"
popd
cd /d "%PROJECT_ROOT%"

set "APP_PORT=8080"
if defined GALGAME_PORT set "APP_PORT=%GALGAME_PORT%"

set "TARGET_PID="

:: 2. Read PID from galgame2voice.pid
if exist "galgame2voice.pid" (
    set /p TARGET_PID=<galgame2voice.pid
    set "TARGET_PID=!TARGET_PID: =!"
)

:: 3. Fallback: Lookup PID from listening port
if not defined TARGET_PID for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":%APP_PORT%"') do if "%%a"=="LISTENING" set "TARGET_PID=%%b"

if not defined TARGET_PID (
    echo [INFO] No running Galgame2Voice instance detected on port %APP_PORT%.
    if exist "galgame2voice.pid" del /f /q galgame2voice.pid >nul 2>&1
    endlocal
    exit /b 0
)

echo Detected Galgame2Voice process [PID: %TARGET_PID%].
echo Sending graceful shutdown signal...

:: 4. Graceful Shutdown Signal
taskkill /PID %TARGET_PID% >nul 2>&1

:: 5. Verification Loop (wait up to 3 seconds)
set "STOP_ATTEMPTS=0"
:STOP_VERIFY_LOOP
timeout /t 1 /nobreak >nul
set /a STOP_ATTEMPTS+=1

tasklist /fi "PID eq %TARGET_PID%" 2>nul | findstr /i "%TARGET_PID%" >nul
if !errorlevel! neq 0 goto STOP_SUCCESS

if !STOP_ATTEMPTS! lss 3 goto STOP_VERIFY_LOOP

:: 6. Force Kill Fallback if still alive
echo Process still responsive after graceful signal, forcing termination...
taskkill /F /PID %TARGET_PID% /T >nul 2>&1

:STOP_SUCCESS
if exist "galgame2voice.pid" del /f /q galgame2voice.pid >nul 2>&1
echo [OK] Galgame2Voice service has been stopped successfully.
endlocal
exit /b 0
