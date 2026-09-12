@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   QQBot + NapCat - 注册开机自启动
echo ========================================
echo.
echo 将注册:
echo   1. NoneBot 登录后立即启动
echo   2. NapCat 大约 45 秒后启动（等 Bot 先起来）
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-autostart.ps1" -StartNow
if errorlevel 1 (
  echo.
  echo 注册失败，请检查上方报错。
  echo 若 NapCat 路径不对，请编辑 scripts\napcat-path.txt
  pause
  exit /b 1
)

echo.
echo 完成。
echo 取消自启请运行: 取消开机自启动.bat
echo.
pause
