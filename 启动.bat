@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

title Galgame2Voice 启动器
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=0"
set "PYTHON_EXE="

REM 1. 优先使用本地虚拟环境
if not exist "%~dp0.venv\Scripts\python.exe" goto :TRY_SYSTEM_PYTHON
"%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 goto :TRY_SYSTEM_PYTHON
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
goto :CHECK_PYTHON

:TRY_SYSTEM_PYTHON
REM 2. 检测系统 python
for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE goto :CHECK_PYTHON

:TRY_PY_3
REM 3. 检测 py -3
for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE goto :CHECK_PYTHON

:TRY_PY
REM 4. 检测 py
for /f "delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE goto :CHECK_PYTHON

goto :NO_PYTHON

:CHECK_PYTHON
REM 验证 Python 能够正常执行且版本 >= 3.10
"%PYTHON_EXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :NO_PYTHON

REM 检查 fastapi 与 uvicorn 核心依赖
"%PYTHON_EXE%" -c "import fastapi, uvicorn, aiosqlite" >nul 2>&1
if not errorlevel 1 goto :DEPS_OK

echo [提示] 正在检查运行依赖... 发现缺少核心运行库: fastapi, uvicorn, aiosqlite
echo [提示] 正在尝试自动安装 requirements.txt 依赖...
"%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
"%PYTHON_EXE%" -c "import fastapi, uvicorn, aiosqlite" >nul 2>&1
if not errorlevel 1 goto :DEPS_OK

echo.
echo ======================================================
echo [错误] 核心运行依赖安装失败
echo 请手动在命令行运行: pip install -r requirements.txt
echo ======================================================
echo.
pause
goto :EXIT_ERR

:DEPS_OK
echo [OK] Python 环境就绪: "%PYTHON_EXE%"
echo.

if not exist "logs" mkdir logs >nul 2>&1
if not exist "data" mkdir data >nul 2>&1
if not exist "audio" mkdir audio >nul 2>&1

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
echo 常见解决办法:
echo 1. 本机尚未安装 Python:
echo    请前往官网下载安装: https://www.python.org/downloads/
echo 2. 安装时未勾选 Add python.exe to PATH:
echo    请重新运行 Python 安装包，务必勾选底部 Add python.exe to PATH
echo 3. 提示弹出微软应用商店:
echo    在 Windows 设置 - 应用 - 应用执行别名 中，将 python.exe 关闭。
echo ======================================================
echo.
pause

:EXIT_ERR
endlocal
exit /b 1
