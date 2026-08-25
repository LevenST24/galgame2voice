@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Galgame2Voice 启动器
echo ══════════════════════════════════════════════════════
echo        🌸 Galgame2Voice 二次元智能语音伴侣
echo                 一键启动器
echo ══════════════════════════════════════════════════════
echo.

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

REM ── 1. 检测 Python 环境 ─────────────────────────────
set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE (
    where python >nul 2>nul
    if !errorlevel! neq 0 (
        echo [错误] 未找到 Python 环境！请先安装 Python 3.10+ 或创建 .venv 虚拟环境。
        pause
        endlocal
        exit /b 1
    )
    set "PYTHON_EXE=python"
)
echo [OK] Python 环境: %PYTHON_EXE%

REM ── 2. 确保运行时目录存在 ───────────────────────────
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "audio" mkdir audio

REM ── 3. 智能启动（自动检测并拉起 GPT-SoVITS，等待就绪）──
REM      run_server.py 会自动完成:
REM        · 检测 9880 端口上的 GPT-SoVITS, 未运行则从
REM          E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604 拉起
REM        · 等待 GPT-SoVITS 模型加载就绪 (有界等待, 日志落盘)
REM        · 启动本服务 (8080, 被占用时自动降级到可用端口)
REM        · 自动打开浏览器

"%PYTHON_EXE%" scripts\run_server.py %*
set "EXIT_CODE=%errorlevel%"
if %EXIT_CODE% neq 0 if %EXIT_CODE% neq 130 if %EXIT_CODE% neq 3221225786 (
    echo.
    echo [错误] 服务异常退出 (错误代码: %EXIT_CODE%)，请查看 logs\galgame2voice.log 排查。
    pause
)

endlocal
exit /b 0
