@echo off
title AC Phone Dashboard
cd /d "%~dp0"
python ac_dash.py
echo.
echo Dashboard stopped. Press any key to close this window.
pause >nul
