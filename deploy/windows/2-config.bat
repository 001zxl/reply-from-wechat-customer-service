@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   还没装环境。请先双击运行 1-install.bat
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   微信客服助手 - 第 2 步：配置
echo ============================================================
echo.
echo   会弹出两个输入框：
echo     1. DeepSeek API Key（必填）
echo     2. 快递100 授权码（可选，直接回车跳过）
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\windows\config.ps1"
if errorlevel 1 (
    echo.
    echo   配置没完成。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   配置完成！接下来双击运行 3-start.bat
echo ============================================================
echo.
pause
