@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

title Galgame2Voice 启动器
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=0"

:: 1. 优先使用本地虚拟环境（若存在且可运行）
set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

:: 2. 若虚拟环境不可用，依次检测系统 python 与 py launcher
if not defined PYTHON_EXE (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON_EXE=py -3"
)

if not defined PYTHON_EXE (
    py -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON_EXE=py"
)

if not defined PYTHON_EXE (
    goto :NO_PYTHON
)

:: 3. 基础依赖检查与自动引导
"%PYTHON_EXE%" -c "import fastapi, uvicorn, aiosqlite" >nul 2>&1
if !errorlevel! neq 0 (
    echo [提示] 正在检查运行依赖... 发现缺少核心库 (fastapi / uvicorn / aiosqlite)
    echo [提示] 正在尝试自动安装依赖 requirements.txt ...
    if exist "%~dp0.venv\Scripts\pip.exe" (
        "%~dp0.venv\Scripts\pip.exe" install -r "%~dp0requirements.txt"
    ) else (
        pip install -r "%~dp0requirements.txt"
    )
    "%PYTHON_EXE%" -c "import fastapi, uvicorn, aiosqlite" >nul 2>&1
    if !errorlevel! neq 0 (
        echo.
        echo ======================================================
        echo [错误] 核心运行依赖安装失败
        echo 请手动在命令行运行: pip install -r requirements.txt
        echo ======================================================
        echo.
        pause
        goto :EXIT_ERR
    )
)

echo [OK] Python 环境就绪: %PYTHON_EXE%
echo.

:: 4. 确保必要运行时目录存在
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "audio" mkdir audio

:: 5. 启动服务端
"%PYTHON_EXE%" scripts\run_server.py %*
set "EXIT_CODE=%errorlevel%"

if "%EXIT_CODE%" == "0" goto :END
if "%EXIT_CODE%" == "130" goto :END
if "%EXIT_CODE%" == "-1073741510" goto :END

echo.
echo ======================================================
echo [错误] 服务已退出，退出码: %EXIT_CODE%
echo 详细运行日志可查看 logs\galgame2voice.log
echo ======================================================
echo.
pause

:END
endlocal
exit /b %EXIT_CODE%

:NO_PYTHON
echo.
echo ======================================================
echo [错误] 未检测到可用的 Python 3.10+ 环境
echo.
echo 常见解决办法：
echo 1. 本机尚未安装 Python:
echo    请前往官网下载安装: https://www.python.org/downloads/
echo 2. 安装时未勾选 Add python.exe to PATH:
echo    请重新运行 Python 安装包，务必勾选底部 Add python.exe to PATH
echo 3. 提示弹出微软应用商店:
echo    在 Windows 设置 - 应用 - 应用执行别名 中，将 python.exe 关闭。
echo ======================================================
echo.
pause
goto :EXIT_ERR

:EXIT_ERR
endlocal
exit /b 1
