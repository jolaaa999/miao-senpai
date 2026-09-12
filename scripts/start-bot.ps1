# 启动深度学习学姐 NoneBot
# 前台：直接运行本脚本
# 自启：由计划任务以 Hidden/Minimized 调用
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$LogFile = Join-Path $LogDir ("bot-" + $stamp + ".log")
$ErrFile = Join-Path $LogDir ("bot-" + $stamp + ".err.log")

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    "$(Get-Date -Format o) ERROR: python not found in PATH" | Out-File -FilePath $ErrFile -Append -Encoding utf8
    throw "找不到 python，请先安装 Python 3.12+ 并加入 PATH"
}
$python = $pythonCmd.Source

$banner = @"
$(Get-Date -Format o) Working dir: $Root
$(Get-Date -Format o) Python: $python
$(Get-Date -Format o) Log: $LogFile
$(Get-Date -Format o) Starting NoneBot...
"@
$banner | Out-File -FilePath $LogFile -Append -Encoding utf8

# bot.py 已内置 loguru 写入 data/logs/bot-YYYYMMDD.log（与终端同内容）
# 自启场景仅将 stderr 单独落盘，避免与文件日志重复
& $python bot.py 2>> $ErrFile
$exitCode = $LASTEXITCODE
"$(Get-Date -Format o) NoneBot exited with code $exitCode" | Out-File -FilePath $LogFile -Append -Encoding utf8
exit $exitCode
