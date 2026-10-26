# 学姐运行期健康检查（只读，不动任何东西）
#
# 为什么要有这个：进阶之后服务变成一堆（bot / NapCat / MCP / 电脑控制），
# 出问题时人会先去翻五个窗口，翻到最后忘了自己在查什么。
# 双击这个脚本，一眼看完「谁活着、有没有连上、她最后一次说话是几点」。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\scripts\health.ps1
#
# 只读：不重启、不发送、不修改配置。

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ok = 0; $warn = 0; $bad = 0
function Report($level, $text) {
    switch ($level) {
        'OK'   { $script:ok++;   Write-Host ("  [OK]   " + $text) -ForegroundColor Green }
        'WARN' { $script:warn++; Write-Host ("  [注意] " + $text) -ForegroundColor Yellow }
        'BAD'  { $script:bad++;  Write-Host ("  [异常] " + $text) -ForegroundColor Red }
        default { Write-Host ("  " + $text) }
    }
}

Write-Host ""
Write-Host "学姐健康检查  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host ("仓库：{0}" -f $Root)

# ---------- 1. 配置里的监听端口 ----------
$envFile = Join-Path $Root '.env'
$port = 27315
$token = ''
if (Test-Path $envFile) {
    $lines = Get-Content $envFile -Encoding UTF8
    foreach ($line in $lines) {
        if ($line -match '^\s*PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
        if ($line -match '^\s*ONEBOT_ACCESS_TOKEN\s*=\s*(.+)$') { $token = $Matches[1].Trim() }
    }
} else {
    Report 'WARN' "没有找到 .env，按默认端口 $port 检查"
}

Write-Host ""
Write-Host "一、进程与端口" -ForegroundColor Cyan

# ---------- 2. bot 进程 ----------
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
# 只认 python 进程：光按 CommandLine 匹配会把"脚本文本里含 bot.py 的 shell"
# 也算进来（我自己就踩过——health 报的 pid 是跑它的那个 powershell）。
$botProc = $procs | Where-Object {
    $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -like '*bot.py*'
}
# 拿不到 CommandLine（权限/时机）时，用端口占用者反查：只要 27315 是某个
# python 占着，那就是她——比"报没在跑"要可靠得多。
if (-not $botProc) {
    $ownerPid = $null
    try {
        $ownerPid = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
            Select-Object -First 1).OwningProcess
    } catch { $ownerPid = $null }
    if (-not $ownerPid) {
        $line = netstat -ano | Select-String (':' + $port) | Select-String 'LISTENING' | Select-Object -First 1
        if ($line) { $ownerPid = [int](($line.ToString() -split '\s+')[-1]) }
    }
    if ($ownerPid) {
        $botProc = $procs | Where-Object { $_.ProcessId -eq $ownerPid }
    }
}
if ($botProc) {
    Report 'OK' ("bot.py 在跑（pid {0}）" -f ($botProc.ProcessId -join ','))
} else {
    Report 'BAD' '没找到 bot.py 进程'
}

# ---------- 3. 端口是否在监听 ----------
# 两步查：Get-NetTCPConnection 在某些时机（刚启动 / 权限不足）会抛异常，
# 抛了就退回 netstat，否则会出现"明明在跑却说没监听"的误报。
$listen = $null
try { $listen = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop } catch { $listen = $null }
if (-not $listen) {
    $listen = netstat -ano | Select-String (':' + $port) | Select-String 'LISTENING' | Select-Object -First 1
}
if ($listen) {
    Report 'OK' ("端口 {0} 正在监听" -f $port)
} else {
    Report 'BAD' ("端口 {0} 没有监听——NapCat 连不进来，她收不到任何消息" -f $port)
}

# ---------- 4. NapCat（如果本机跑） ----------
$napcat = Get-CimInstance Win32_Process -Filter "Name='QQ.exe' OR Name='NapCatWinBootMain.exe' OR Name='node.exe'" |
    Where-Object { $_.CommandLine -like '*napcat*' }
if ($napcat) { Report 'OK' 'NapCat 进程在跑' } else { Report 'WARN' '没看到 NapCat 进程（可能跑在别的机器上）' }

Write-Host ""
Write-Host "二、她最后动过什么时候" -ForegroundColor Cyan

# ---------- 5. 数据目录新鲜度 ----------
$watch = @(
    @{ Path = 'data\dl_senpai\memory';       Name = '对话记忆' },
    @{ Path = 'data\dl_senpai\recent_chat';  Name = '最近聊天流水' },
    @{ Path = 'data\dl_senpai\diary';        Name = '日记' },
    @{ Path = 'data\dl_senpai\own_life';     Name = '自己的日子' }
)
foreach ($item in $watch) {
    $full = Join-Path $Root $item.Path
    if (-not (Test-Path $full)) { Report 'WARN' ("{0}：目录还不存在（{1}）" -f $item.Name, $item.Path); continue }
    $newest = Get-ChildItem $full -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { Report 'WARN' ("{0}：空目录" -f $item.Name); continue }
    $mins = [int]((Get-Date) - $newest.LastWriteTime).TotalMinutes
    $detail = "{0}：最后写入 {1} 分钟前（{2}）" -f $item.Name, $mins, $newest.Name
    if ($mins -gt 24 * 60) { Report 'WARN' $detail } else { Report 'OK' $detail }
}

# ---------- 6. 日志里的错误尾巴 ----------
Write-Host ""
Write-Host "三、日志" -ForegroundColor Cyan
$logDir = Join-Path $Root 'data\logs'
$newestLog = Get-ChildItem $logDir -Filter 'bot-*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newestLog) {
    $mins = [int]((Get-Date) - $newestLog.LastWriteTime).TotalMinutes
    Report 'OK' ("最新日志 {0}（{1} 分钟前写入）" -f $newestLog.Name, $mins)
    $errs = Select-String -Path $newestLog.FullName -Pattern 'ERROR|Traceback|500 Internal' -SimpleMatch:$false |
        Select-Object -Last 3
    if ($errs) {
        Report 'WARN' ("最近有 {0} 条错误，最后 3 条：" -f (($errs | Measure-Object).Count))
        $errs | ForEach-Object { Write-Host ("         " + $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length))) -ForegroundColor DarkYellow }
    } else {
        Report 'OK' '最新日志里没有 ERROR / Traceback'
    }
} else {
    Report 'WARN' 'data\logs 里没有 bot-*.log（还没跑过或日志被轮转掉了）'
}

# ---------- 7. 电脑控制围栏状态 ----------
Write-Host ""
Write-Host "四、电脑控制（默认关）" -ForegroundColor Cyan
$cfgText = if (Test-Path $envFile) { Get-Content $envFile -Raw -Encoding UTF8 } else { '' }
$compEnable = ($cfgText -match 'DL_SENPAI_COMPUTER_ENABLE\s*=\s*true')
$compMode = if ($cfgText -match 'DL_SENPAI_COMPUTER_MODE\s*=\s*([A-Za-z]+)') { $Matches[1] } else { 'view' }
if (-not $compEnable) {
    Report 'OK' '电脑控制未开启（这是默认状态）'
} else {
    Report 'WARN' ("电脑控制已开启，模式 {0}；确认接的是模拟器/备用机，不是主力机" -f $compMode)
    if ($cfgText -match 'DL_SENPAI_COMPUTER_ALLOW_APPS\s*=\s*(\S+)') {
        Report 'OK' ("白名单：{0}" -f $Matches[1])
    } else {
        Report 'BAD' '白名单是空的——此时她可以看，但任何点击/输入都会被拒'
    }
    $leaseFile = Join-Path $Root 'data\dl_senpai\computer\lease.json'
    if (Test-Path $leaseFile) {
        $lease = Get-Content $leaseFile -Raw | ConvertFrom-Json
        $remain = [int]([double]$lease.until - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
        if ($remain -gt 0) { Report 'WARN' ("租约还有 {0} 秒——她现在真的能动鼠标" -f $remain) }
        else { Report 'OK' '租约已过期（当前不可操作）' }
    } else {
        Report 'OK' '没有租约文件（当前不可操作）'
    }
}

# ---------- 8. 结论 ----------
Write-Host ""
Write-Host ("结论：正常 {0} / 注意 {1} / 异常 {2}" -f $ok, $warn, $bad) -ForegroundColor Cyan
if ($bad -gt 0) {
    Write-Host "先看「异常」那几行；只读检查不会改动任何东西，放心反复跑。" -ForegroundColor Red
} elseif ($warn -gt 0) {
    Write-Host "没有硬故障，注意项可以慢慢看。" -ForegroundColor Yellow
} else {
    Write-Host "全都正常。" -ForegroundColor Green
}
Write-Host ""
