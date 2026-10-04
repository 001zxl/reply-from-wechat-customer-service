@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title 微信客服助手 - 环境诊断

echo.
echo ============================================================
echo   环境诊断
echo ============================================================
echo.
echo   收集运行这个程序所需的电脑信息，生成一份报告。
echo.
echo   报告里没有你的 API Key，可以放心发给对方。
echo   过程中微信窗口会闪一下、会被截几张图，正常现象。
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\windows\diagnose.ps1"

echo.
echo   开始生成报告和截图列表...
echo.
if exist "diagnose-report.txt" start "" notepad "diagnose-report.txt"
if exist "C:\Windows\Temp\wechat_cs_probe" explorer "C:\Windows\Temp\wechat_cs_probe"

echo.
pause
