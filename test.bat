@echo off
cd /d "%~dp0"
echo ============================================
echo   CommandCenter - Running test suite
echo ============================================
echo.
pip install pytest --quiet
python -m pytest tests -v %*
pause
