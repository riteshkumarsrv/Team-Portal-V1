@echo off
cd /d "%~dp0"
set PORT=%FLASK_RUN_PORT%
if "%PORT%"=="" set PORT=5000
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:%PORT%/"
python app.py
if errorlevel 1 pause
