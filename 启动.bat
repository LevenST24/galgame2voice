@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Galgame2Voice 启动器
echo ======================================================
echo          Galgame2Voice - 女主伴侣服务启动中
echo ======================================================
echo.

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=0"

:: 1. Detect Python and Dependencies
set "PYTHON_EXE="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    "%PYTHON_EXE%" -c "import fastapi, uvicorn, aiosqlite" >nul 2>nul
    if !errorlevel! neq 0 (
        echo [提示] 正在补全缺失的运行依赖...
        if exist "%~dp0requirements.txt" (
            "%~dp0.venv\Scripts\pip.exe" install -r "%~dp0requirements.txt"
        )
    )
)

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if !errorlevel! neq 0 (
        echo [错误] 未找到 Python 环境！
        echo 请先安装 Python 3.10+ 并勾选 Add Python to PATH
        echo 官网下载: https://www.python.org/downloads/
        echo.
        pause
        endlocal
        exit /b 1
    )

    echo [首次部署] 正在自动初始化虚拟环境 .venv ...
    where uv >nul 2>nul
    if !errorlevel! equ 0 (
        uv venv "%~dp0.venv" --allow-existing
        if exist "%~dp0.venv\Scripts\python.exe" (
            echo [首次部署] 正在通过 uv 高速安装依赖...
            uv pip install -r "%~dp0requirements.txt" --python "%~dp0.venv\Scripts\python.exe"
            set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
        )
    )
    if not defined PYTHON_EXE (
        python -m venv "%~dp0.venv"
        if exist "%~dp0.venv\Scripts\python.exe" (
            echo [首次部署] 正在安装运行依赖 requirements.txt ...
            "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
            set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
        ) else (
            echo [警告] 创建虚拟环境失败，尝试使用系统 Python 环境...
            set "PYTHON_EXE=python"
            pip install -r "%~dp0requirements.txt"
        )
    )
)

echo [OK] Python 环境: %PYTHON_EXE%
echo.

:: 2. Ensure Runtime Directories Exist
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "audio" mkdir audio

:: 3. Launch Server via run_server.py
"%PYTHON_EXE%" scripts\run_server.py %*
set "EXIT_CODE=!errorlevel!"
if !EXIT_CODE! neq 0 (
    if !EXIT_CODE! neq 130 (
        if !EXIT_CODE! neq 3221225786 (
            if !EXIT_CODE! neq -1073741510 (
                echo.
                echo [错误] 服务异常退出 (错误代码: !EXIT_CODE!)，请查看 logs\galgame2voice.log 排查。
                pause
            )
        )
    )
)

endlocal
exit /b !EXIT_CODE!
