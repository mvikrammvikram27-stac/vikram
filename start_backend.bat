@echo off
echo ==========================================
echo   The V-Cafe Backend Server
echo   Running on http://localhost:5000
echo ==========================================
echo.
echo Starting server... Press Ctrl+C to stop.
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0backend\start_server.ps1"
pause
