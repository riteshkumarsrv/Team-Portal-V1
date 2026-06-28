# Deploy Team Portal V1 to Fly.io (run from repo root).
# Prerequisites: flyctl installed, `fly auth login` completed.
# Usage:
#   $env:MANAGER_DASHBOARD_PASSWORD = 'your-strong-password'
#   .\scripts\fly-deploy.ps1

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command fly -ErrorAction SilentlyContinue)) {
    Write-Error "Fly CLI not found. Install: winget install fly.io.flyctl"
}

$app = 'team-portal-v1'
$region = 'bom'
$volume = 'team_portal_data'

fly auth whoami | Out-Null

if (-not (fly apps list 2>$null | Select-String -SimpleMatch $app)) {
    Write-Host "Creating Fly app $app ..."
    fly apps create $app --yes
}

$volList = fly volumes list -a $app 2>$null
if (-not ($volList | Select-String -SimpleMatch $volume)) {
    Write-Host "Creating volume $volume in $region (1 GB) ..."
    fly volumes create $volume -a $app --region $region --size 1 --yes
}

if (-not $env:FLASK_SECRET_KEY) {
    $env:FLASK_SECRET_KEY = [Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Maximum 256 }))
    Write-Host "Generated FLASK_SECRET_KEY for this deploy (stored in Fly secrets)."
}

if (-not $env:MANAGER_DASHBOARD_PASSWORD) {
    Write-Error "Set MANAGER_DASHBOARD_PASSWORD before deploy, e.g. `$env:MANAGER_DASHBOARD_PASSWORD = 'your-password'"
}

Write-Host "Setting Fly secrets ..."
fly secrets set `
    FLASK_SECRET_KEY="$env:FLASK_SECRET_KEY" `
    MANAGER_DASHBOARD_PASSWORD="$env:MANAGER_DASHBOARD_PASSWORD" `
    TEAM_TRACKER_PRODUCTION=1 `
    TEAM_TRACKER_DB_PATH=/app/data/team_tracker.db `
    -a $app

Write-Host "Deploying ..."
fly deploy -a $app

Write-Host ""
Write-Host "Done. Open:" (fly info -a $app | Select-String 'Hostname').ToString().Replace('Hostname','').Trim()
Write-Host "Manager login: https://<hostname>/login"
