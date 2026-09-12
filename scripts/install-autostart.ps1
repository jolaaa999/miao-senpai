# Register Windows logon autostart for NoneBot + NapCat
param(
    [switch]$StartNow,
    [switch]$BotOnly,
    [switch]$NapCatOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BotScript = Join-Path $Root "scripts\start-bot.ps1"
$NapCatScript = Join-Path $Root "scripts\start-napcat.ps1"
$BotTask = "QQBot-DL-Senpai"
$NapCatTask = "QQBot-NapCat"

if (-not (Test-Path $BotScript)) { throw "Missing $BotScript" }
if (-not (Test-Path $NapCatScript)) { throw "Missing $NapCatScript" }

function Register-LogonTask {
    param(
        [string]$TaskName,
        [string]$ScriptPath,
        [string]$Description
    )
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $Description | Out-Null
    Write-Host "Registered: $TaskName"
}

$doBot = -not $NapCatOnly
$doNapCat = -not $BotOnly

if ($doBot) {
    Register-LogonTask -TaskName $BotTask -ScriptPath $BotScript -Description "QQBot DL Senpai NoneBot auto start at logon"
}
if ($doNapCat) {
    Register-LogonTask -TaskName $NapCatTask -ScriptPath $NapCatScript -Description "QQBot NapCat auto start at logon (delayed)"
}

Write-Host ""
Write-Host "Root: $Root"
Write-Host "NapCat path file: $(Join-Path $Root 'scripts\napcat-path.txt')"
Write-Host "Logs: $(Join-Path $Root 'data\logs')"
Write-Host ""
Write-Host "At Windows logon:"
if ($doBot) { Write-Host "  1) $BotTask starts NoneBot immediately" }
if ($doNapCat) { Write-Host "  2) $NapCatTask starts NapCat after ~45s (waits for Bot)" }
Write-Host ""
Write-Host "Start now:  Start-ScheduledTask -TaskName $BotTask; Start-ScheduledTask -TaskName $NapCatTask"
Write-Host "Uninstall:  .\scripts\uninstall-autostart.ps1"

if ($StartNow) {
    if ($doBot) {
        Start-ScheduledTask -TaskName $BotTask
        Write-Host "Started: $BotTask"
    }
    if ($doNapCat) {
        Start-ScheduledTask -TaskName $NapCatTask
        Write-Host "Started: $NapCatTask (NapCat will wait ~45s)"
    }
}
