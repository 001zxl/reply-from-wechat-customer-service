@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title 微信客服助手 - 安装

echo.
echo ============================================================
echo   微信客服助手  -  第 1 步：安装环境
echo ============================================================
echo.
echo   自动完成：找/装 Python 3.12 - 建独立环境 - 装依赖包
echo   大约 5-15 分钟（看网速）。中途不要关窗口。
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\windows\install.ps1"

echo.
pause
