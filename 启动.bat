@echo off
cd /d "%~dp0"
echo Starting... keep this window open.
where python >nul 2>nul
if %errorlevel%==0 (
    python server.py
    goto end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py server.py
    goto end
)
echo Python not found. Install Python 3 and tick Add Python to PATH: https://www.python.org/downloads/
:end
pause
