@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title 停止 Galgame2Voice
echo ========================================================
echo        正在停止 Galgame2Voice 与 GPT-SoVITS
echo ========================================================
echo.

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

REM 1. 终止 Galgame2Voice 服务 (端口 8080 及动态端口)
echo [1/2] 正在关闭 Galgame2Voice 服务 [端口 8080]...
for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":8080"') do (
    if "%%a"=="LISTENING" (
        taskkill /f /t /pid %%b >nul 2>&1
        echo   [OK] 已终止端口 8080 进程 PID: %%b
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

REM 2. 终止 GPT-SoVITS 语音引擎 (端口 9880, 释放显存)
echo [2/2] 正在关闭 GPT-SoVITS 语音引擎 [端口 9880]...
for /f "tokens=4,5" %%a in ('netstat -ano ^| findstr ":9880"') do (
    if "%%a"=="LISTENING" (
        taskkill /f /t /pid %%b >nul 2>&1
        echo   [OK] 已终止端口 9880 进程 PID: %%b
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
echo   [OK] 全部服务已停止，端口已释放，显存已归还！
echo ========================================================
echo.

endlocal
exit /b 0
