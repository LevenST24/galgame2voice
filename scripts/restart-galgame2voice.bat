@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title Restart Galgame2Voice
echo ========================================================
echo            Restarting Galgame2Voice Service
echo ========================================================
echo.

set "SCRIPT_DIR=%~dp0"
call "%~dp0stop-galgame2voice.bat"
timeout /t 2 /nobreak >nul
call "%~dp0start-galgame2voice.bat"

endlocal
exit /b 0
