@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" scripts\run_server.py %*
) else (
    call python scripts\run_server.py %*
)
if "%ERRORLEVEL%"=="9009" echo 没有找到 Python。这个文件夹是给开发者用的源码包；普通用户请到发布页下载文件名带 windows-x64 的压缩包，解压后双击里面的「启动.bat」。
if errorlevel 1 pause
