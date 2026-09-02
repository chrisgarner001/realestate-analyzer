@echo off
title PropYield Server
cd /d "%~dp0"

:: Kill anything already on port 8000
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8000 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

:: Set encoding so Unicode chars don't crash the server
set PYTHONIOENCODING=utf-8
chcp 65001 >nul

echo.
echo  PropYield Server starting...
echo  Open: http://localhost:8000/test
echo.

python -m uvicorn server:app --host 0.0.0.0 --port 8000

echo.
echo Server stopped. Press any key to exit.
pause >nul
