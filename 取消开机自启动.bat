@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   QQBot 学姐 - 取消开机自启动
echo ========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall-autostart.ps1"
echo.
pause
