# Unregister NoneBot + NapCat autostart tasks
$ErrorActionPreference = "Stop"
$names = @("QQBot-DL-Senpai", "QQBot-NapCat")

foreach ($TaskName in $names) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Task not found: $TaskName"
        continue
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered: $TaskName"
}
