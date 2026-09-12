# 启动学姐 Web 控制台（Go 后端 + Vue 前端）
# 日常推荐直接 python bot.py（会附带拉起控制台）
# 本脚本仅单独起控制台、不起 bot

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path "$Root\bot.py")) {
    $Root = "e:\PROJECT\QQBot"
}

$env:QQBOT_ROOT = $Root

Write-Host "项目根: $Root" -ForegroundColor Cyan
Write-Host "后端 API: http://127.0.0.1:28473/x7k9-dl-senpai-console/v1" -ForegroundColor Green
Write-Host "前端 UI:  http://127.0.0.1:41788" -ForegroundColor Green
Write-Host "提示: python bot.py 会默认一并启动本控制台" -ForegroundColor DarkGray

# 后端
$backendJob = Start-Job -ScriptBlock {
    param($root)
    $env:QQBOT_ROOT = $root
    Set-Location "$root\admin\backend"
    $env:CGO_ENABLED = "0"
    go run ./cmd/server/
} -ArgumentList $Root

Start-Sleep -Seconds 2

# 前端
Set-Location "$Root\admin\frontend"
if (-not (Test-Path "node_modules")) {
    Write-Host "首次运行，安装前端依赖…" -ForegroundColor Yellow
    npm install
}
npm run dev

# 若前端退出，结束后端
Stop-Job $backendJob -ErrorAction SilentlyContinue
Remove-Job $backendJob -ErrorAction SilentlyContinue
