@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title 微信客服助手 - 运行中（别关这个窗口）

if not exist ".venv\Scripts\python.exe" (
    echo   还没装环境，请先运行 1-install.bat
    pause
    exit /b 1
)
if not exist ".env" (
    echo   还没配置，请先运行 2-config.bat
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   微信客服助手 正在启动
echo ============================================================
echo.
echo   +--------------------------------------------------+
echo   ^|  这个黑窗口是程序本体，使用期间请不要关闭。       ^|
echo   ^|  关掉它 = 停止服务                               ^|
echo   ^|  要停止：在这里按 Ctrl+C                         ^|
echo   +--------------------------------------------------+
echo.
echo   几秒后会自动打开浏览器...
echo.

start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 6; Start-Process 'http://127.0.0.1:8787/desk'"

".venv\Scripts\python.exe" -m uvicorn app.server:app --host 127.0.0.1 --port 8787

echo.
echo   服务已停止。
pause
