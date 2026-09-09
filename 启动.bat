@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

title Galgame2Voice 启动器
echo ======================================================
echo          Galgame2Voice - 女主伴侣服务启动中
echo ======================================================
echo.

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=0"

:: 1. 检查已有的 .venv 是否真正可运行（防止从其他电脑复制过来的虚拟环境失效）
set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    ) else (
        echo [提示] 检测到已损坏或跨机器复制的 .venv，正在重新构建...
        rmdir /s /q "%~dp0.venv" >nul 2>&1
    )
)

:: 2. 如果没有可用 .venv，检测系统 Python
if not defined PYTHON_EXE (
    echo [环境检查] 正在检测 Python 环境...
    
    :: 真正执行 python 检查，避开 Windows 商店空快捷方式陷阱
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if !errorlevel! neq 0 (
        echo.
        echo ======================================================
        echo [错误] 未检测到可用的 Python (建议 3.10+)！
        echo.
        echo 常见原因与解决办法：
        echo 1. 本电脑尚未安装 Python：
        echo    请前往官网下载安装: https://www.python.org/downloads/
        echo 2. 安装时未勾选 "Add python.exe to PATH"：
        echo    请重新运行 Python 安装包，务必勾选底部 "Add python.exe to PATH"！
        echo 3. 提示弹出微软应用商店：
        echo    在 Windows 设置 -> 应用 -> 应用执行别名 中，把 python.exe 关掉。
        echo ======================================================
        echo.
        pause
        exit /b 1
    )

    echo [首次部署] 正在初始化本地虚拟环境 .venv ...
    where uv >nul 2>&1
    if !errorlevel! equ 0 (
        uv venv "%~dp0.venv" --allow-existing >nul 2>&1
        if exist "%~dp0.venv\Scripts\python.exe" (
            echo [首次部署] 正在通过 uv 极速安装依赖 requirements.txt ...
            uv pip install -r "%~dp0requirements.txt" --python "%~dp0.venv\Scripts\python.exe"
            set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
        )
    )

    if not defined PYTHON_EXE (
        python -m venv "%~dp0.venv"
        if exist "%~dp0.venv\Scripts\python.exe" (
            echo [首次部署] 正在安装运行依赖 requirements.txt (首次约需 1~2 分钟)...
            "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
            set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
        ) else (
            echo [警告] 虚拟环境创建失败，尝试直接使用系统环境...
            set "PYTHON_EXE=python"
            pip install -r "%~dp0requirements.txt"
        )
    )
)

:: 3. 依赖快速自检（缺失则自动补全）
"%PYTHON_EXE%" -c "import fastapi, uvicorn, aiosqlite" >nul 2>&1
if !errorlevel! neq 0 (
    echo [提示] 正在补全缺失的基础运行依赖...
    if exist "%~dp0.venv\Scripts\pip.exe" (
        "%~dp0.venv\Scripts\pip.exe" install -r "%~dp0requirements.txt"
    ) else (
        pip install -r "%~dp0requirements.txt"
    )
)

echo [OK] Python 环境就绪: %PYTHON_EXE%
echo.

:: 4. 确保必要数据目录存在
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "audio" mkdir audio

:: 5. 启动服务端
"%PYTHON_EXE%" scripts\run_server.py %*
set "EXIT_CODE=%errorlevel%"

:: 6. 异常退出时暂停窗口展示错误信息，杜绝黑框闪退
if "%EXIT_CODE%" neq "0" (
    if "%EXIT_CODE%" neq "130" (
        if "%EXIT_CODE%" neq "-1073741510" (
            echo.
            echo ======================================================
            echo [错误] 服务异常退出 (退出码: %EXIT_CODE%)
            echo 详细错误日志可查看 logs\galgame2voice.log
            echo ======================================================
            echo.
            pause
        )
    )
)

endlocal
exit /b %EXIT_CODE%
