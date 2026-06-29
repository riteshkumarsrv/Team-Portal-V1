@echo off
:: Daily live-DB backup runner for Windows Task Scheduler.
:: Double-click to run manually, or point Task Scheduler at this file.
set PA_USERNAME=TeamPortal
set PA_API_TOKEN=344e28f44783456bcea2f6c2e37cf571539156f1
cd /d "%~dp0.."
python scripts\backup_live_db.py >> LiveDatabaseBackup\backup.log 2>&1
