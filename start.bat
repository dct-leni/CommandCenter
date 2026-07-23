@echo off
cd /d "%~dp0"
echo ============================================
echo   CommandCenter - Video Converter ^& Streamer
echo ============================================
echo.
echo Installing dependencies...
pip install -r requirements.txt --quiet
echo.
echo Starting CommandCenter...
python -m app.main
pause

