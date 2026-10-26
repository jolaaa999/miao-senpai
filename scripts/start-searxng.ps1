# 启动本机 SearXNG 容器（学姐联网检索的可选上游）
#
# 为什么值得自建：公共 SearXNG 实例基本都关了 JSON 接口或被限流
# （实测 12 个公共实例，0 个能用）。自建后可以聚合 Google/Bing 等引擎，
# 免费、无限次、带本地缓存，比裸抓 SERP 稳得多。
#
# 前置：Docker Desktop 已在运行（任务栏鲸鱼图标常亮）。本脚本不会替你启动它。
# 用法：pwsh -File scripts\start-searxng.ps1
#       pwsh -File scripts\start-searxng.ps1 -Stop

param(
    # 8080 在本机被别的进程占用（CEF remote debugging），默认改用 8090
    [int]$Port = 8090,
    [string]$ContainerName = "senpai-searxng",
    [switch]$Stop
)

# docker 命令失败时会往 stderr 写字，PowerShell 在 Stop 模式下会把它当异常
# 直接终止脚本（比如「容器不存在」这种无害错误），所以这里用 Continue 自行判断。
$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $PSScriptRoot
$configDir = Join-Path $repoRoot "searxng"

if ($Stop) {
    docker rm -f $ContainerName *> $null
    Write-Host "已停止并删除容器 $ContainerName"
    exit 0
}

# daemon 没起来时 docker 命令会直接报错，这里先探一下给出人话提示
docker info --format "{{.ServerVersion}}" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker Desktop 没在运行。先手动启动它（开始菜单搜 Docker Desktop），等鲸鱼图标稳定后再跑本脚本。" -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir | Out-Null
}

$settingsPath = Join-Path $configDir "settings.yml"
if (-not (Test-Path $settingsPath)) {
    # secret_key 每次生成新的：SearXNG 不接受默认值，缺了起不来
    $chars = (48..57) + (97..122)
    $secret = -join ($chars | Get-Random -Count 32 | ForEach-Object { [char]$_ })
    $yaml = @"
use_default_settings: true

server:
  secret_key: "$secret"
  # 只给本机学姐用，不开限流和公开实例标记
  limiter: false
  public_instance: false

search:
  safe_search: 0
  # 学姐走的是 JSON 接口，默认只开 html 会拿不到数据
  formats:
    - html
    - json
"@
    Set-Content -Path $settingsPath -Value $yaml -Encoding UTF8
    Write-Host "已生成配置：$settingsPath"
}

$portMap = [string]::Concat($Port, ":8080")
$volumeMap = [string]::Concat($configDir, ":/etc/searxng")
$baseUrl = [string]::Concat("BASE_URL=http://127.0.0.1:", $Port, "/")

docker rm -f $ContainerName *> $null
$dockerArgs = @(
    "run", "-d", "--name", $ContainerName,
    "-p", $portMap,
    "-v", $volumeMap,
    "-e", $baseUrl,
    "-e", "INSTANCE_NAME=senpai-search",
    "--restart", "unless-stopped",
    "searxng/searxng:latest"
)
& docker @dockerArgs | Out-Null

Write-Host "容器已启动，等它就绪（首次拉镜像可能要几分钟）…"
$probeUrl = [string]::Concat("http://127.0.0.1:", $Port, "/search?q=test&format=json")
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 3
    try {
        $resp = Invoke-RestMethod -Uri $probeUrl -TimeoutSec 8
        if ($null -ne $resp.results) { $ready = $true; break }
    } catch { }
}

if (-not $ready) {
    Write-Host "40 次轮询内没拿到 JSON 结果，看日志：docker logs $ContainerName" -ForegroundColor Yellow
    exit 1
}

Write-Host "SearXNG 就绪：http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "接下来把这一行写进 .env，然后重启机器人："
Write-Host "  DL_SENPAI_SEARCH_SEARXNG_URL=http://127.0.0.1:$Port"
