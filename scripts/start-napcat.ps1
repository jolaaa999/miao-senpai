# Start NapCatQQ Desktop (for scheduled autostart)
param(
    [int]$DelaySeconds = 45
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("napcat-" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-Log([string]$msg) {
    "$(Get-Date -Format o) $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

function Resolve-NapCatExe {
    if ($env:NAPCAT_EXE -and (Test-Path $env:NAPCAT_EXE)) {
        return $env:NAPCAT_EXE
    }
    $cfg = Join-Path $Root "scripts\napcat-path.txt"
    if (Test-Path $cfg) {
        $p = (Get-Content $cfg -Raw).Trim().Trim('"')
        if ($p -and (Test-Path $p)) { return $p }
    }
    $candidates = @(
        "E:\NapCatQQ_Desktop\NapCatQQ-Desktop.exe",
        "D:\NapCatQQ_Desktop\NapCatQQ-Desktop.exe",
        "C:\NapCatQQ_Desktop\NapCatQQ-Desktop.exe",
        (Join-Path $env:LOCALAPPDATA "NapCatQQ_Desktop\NapCatQQ-Desktop.exe"),
        (Join-Path $env:USERPROFILE "NapCatQQ_Desktop\NapCatQQ-Desktop.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

$exe = Resolve-NapCatExe
if (-not $exe) {
    Write-Log "ERROR: NapCat executable not found. Put full path into scripts\napcat-path.txt"
    throw "NapCat executable not found"
}

# Wait so NoneBot can listen first
if ($DelaySeconds -gt 0) {
    Write-Log "Waiting $DelaySeconds seconds before starting NapCat..."
    Start-Sleep -Seconds $DelaySeconds
}

$running = Get-Process -Name "NapCatQQ-Desktop" -ErrorAction SilentlyContinue
if ($running) {
    Write-Log "NapCat already running (pid=$($running.Id -join ',')). Skip."
    exit 0
}

Write-Log "Starting: $exe"
$workdir = Split-Path -Parent $exe
Start-Process -FilePath $exe -WorkingDirectory $workdir
Write-Log "NapCat start requested."
