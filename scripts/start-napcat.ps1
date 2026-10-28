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

# 外壳不在，但内核可能还活着：实测过外壳退出后 SnowLuma 的 node 会裸跑，
# 并因为写日志的管道断了疯狂刷 EPIPE（一分钟 50MB，最后把 WebSocket 拖断）。
# 这种孤儿必须先清掉，否则新外壳起来会和它抢端口/双开。
$orphans = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "node.exe" -and $_.ExecutablePath -like "*NapCatQQ Desktop*"
}
foreach ($o in $orphans) {
    Write-Log "Killing orphan SnowLuma kernel pid=$($o.ProcessId)"
    try { Stop-Process -Id $o.ProcessId -Force -ErrorAction Stop }
    catch { Write-Log "ERROR: kill $($o.ProcessId) failed: $($_.Exception.Message)" }
}
if ($orphans) { Start-Sleep -Seconds 3 }

Write-Log "Starting: $exe"
$workdir = Split-Path -Parent $exe
Start-Process -FilePath $exe -WorkingDirectory $workdir
Write-Log "NapCat start requested."
