# 学姐（bot.py）看守：端口没人监听就拉起来。
# 为什么看端口而不是看进程：进程活着但 uvicorn 没起来（bind 失败、启动中途崩）时，
# 进程照样在，只看进程会永远判断"健康"。端口 LISTENING 才是真的能收消息。
param(
    [int]$Port = 27315,
    # 启动宽限：实测冷启动到 uvicorn 监听要 3.5 分钟（10:41:35 起、10:45:11 才连上），
    # 机器负载高时更久。原来给 120 秒，结果启动中的进程被当成僵死杀掉再拉起，
    # 形成「杀—起—再杀」的死循环（2026-10-28 重启用实际撞到过）。给到 5 分钟。
    [int]$StartupGraceSeconds = 300
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("watchdog-bot-" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-Log([string]$msg) {
    "$(Get-Date -Format o) $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

function Test-PortListening([int]$p) {
    # netstat 输出在中文 Windows 是 GBK，这里用 CIM 的 OwningProcess 更稳；
    # 但 Get-NetTCPConnection 在部分精简系统缺失，所以先试它、失败再回退 netstat。
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction Stop
        return [bool]$conn
    } catch {
        $out = (& netstat -ano -p TCP | Out-String)
        return ($out -match (":" + $p + "\s")) -and ($out -match "LISTENING")
    }
}

if (Test-PortListening $Port) {
    Write-Log "bot port $Port LISTENING. Skip."
    exit 0
}

# 端口没在听：看是不是刚启动（uvicorn 起来要几秒到几十秒，别在这期间反复重启）
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*bot.py*" })
if ($procs.Count -gt 0) {
    $newest = ($procs | Sort-Object CreationDate -Descending | Select-Object -First 1)
    $age = ((Get-Date) - $newest.CreationDate).TotalSeconds
    if ($age -lt $StartupGraceSeconds) {
        Write-Log ("bot.py pid=" + $newest.ProcessId + " 启动中 (" + [int]$age + "s)，先不管。")
        exit 0
    }
    # 活着但端口一直没起来 = 僵死（bind 失败/启动崩），杀掉重来
    foreach ($p in $procs) {
        Write-Log ("bot.py pid=" + $p.ProcessId + " 存活但端口没听，结束它。")
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }
        catch { Write-Log ("ERROR: kill " + $p.ProcessId + " failed: " + $_.Exception.Message) }
    }
    Start-Sleep -Seconds 3
}

$starter = Join-Path $Root "scripts\start-bot.ps1"
if (-not (Test-Path $starter)) {
    Write-Log "ERROR: 找不到 $starter"
    exit 1
}
Write-Log "Starting bot via $starter"
Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $starter) -WorkingDirectory $Root -WindowStyle Hidden
Write-Log "bot start requested."