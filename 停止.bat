@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PYTHON_EXE=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"

"%PYTHON_EXE%" scripts\launcher.py stop %*
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to stop service.
    pause
)
endlocal
