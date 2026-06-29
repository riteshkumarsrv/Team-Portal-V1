# Run this script ONCE (as Administrator) to register a daily backup task
# in Windows Task Scheduler.
#
#   Right-click PowerShell → "Run as Administrator", then:
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_daily_backup.ps1

$taskName   = "SCRUM_vF_LiveDB_Backup"
$batFile    = (Resolve-Path "$PSScriptRoot\backup_live_db.bat").Path
$triggerTime = "02:00"   # runs at 2 AM daily — change as needed

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batFile`""
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName   $taskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Force

Write-Host ""
Write-Host "Task '$taskName' registered - runs daily at $triggerTime."
Write-Host "To run it immediately: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "To remove it:          Unregister-ScheduledTask -TaskName '$taskName' -Confirm:false"
