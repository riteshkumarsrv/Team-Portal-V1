# Starts the team website server and opens it in your default browser.
# Usage: right-click -> Run with PowerShell, or: powershell -ExecutionPolicy Bypass -File .\open-website.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$port = if ($env:FLASK_RUN_PORT) { $env:FLASK_RUN_PORT } else { "5000" }
$url = "http://127.0.0.1:$port/"

# Open browser shortly after the dev server begins listening.
Start-Process powershell -ArgumentList @(
    "-NoProfile",
    "-WindowStyle", "Hidden",
    "-Command",
    "Start-Sleep -Seconds 2; if (Get-Command Start-Process -ErrorAction SilentlyContinue) { Start-Process '$url' }"
) | Out-Null

python app.py
