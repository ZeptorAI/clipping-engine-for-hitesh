@echo off
cd /d "%~dp0"
echo ============================================
echo   Clip Editor - one-time setup
echo ============================================
echo.
echo Installing Python dependencies...
python -m pip install --quiet --upgrade flask anthropic requests pillow
if errorlevel 1 (
  echo.
  echo ERROR: Could not install dependencies. Is Python installed?
  echo Get it from https://python.org and check "Add to PATH".
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo WARNING: ffmpeg was not found.
  echo Download it from https://www.gyan.dev/ffmpeg/builds/ and add it to PATH.
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Created .env - open it and paste your API key.
)

echo.
echo Setup complete. Now open .env, paste your API key, and run start.bat
echo.
pause
