@echo off
cd /d "%~dp0"
title Clip Editor
echo ============================================
echo   Clip Editor
echo ============================================
echo.
echo Checking for updates...
git pull --quiet
echo.
echo Keep THIS window open while you use the app.
echo Close it when you're done to stop the app.
echo.
echo Opening http://127.0.0.1:5000 in your browser...
start "" http://127.0.0.1:5000
echo.
python app.py
echo.
echo The app has stopped.
pause
