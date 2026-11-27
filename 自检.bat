@echo off
chcp 65001 >nul
title 金相 · 水贝工作台 自检
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python 自检.py
    goto end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py 自检.py
    goto end
)

echo   [!] 没找到 Python。
echo   请先安装 Python 3（安装时勾选 Add Python to PATH）：
echo   https://www.python.org/downloads/
pause

:end
