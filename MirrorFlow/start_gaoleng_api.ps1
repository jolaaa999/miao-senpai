# 启动「高冷女神」微调模型的 OpenAI 兼容 API（默认 http://127.0.0.1:8001）
# 用法（在 QQBot 根目录或任意处）：
#   powershell -ExecutionPolicy Bypass -File MirrorFlow\start_gaoleng_api.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:API_HOST = if ($env:GAOLENG_API_HOST) { $env:GAOLENG_API_HOST } else { "127.0.0.1" }
$env:API_PORT = if ($env:GAOLENG_API_PORT) { $env:GAOLENG_API_PORT } else { "8001" }
# 强制用本地风格密钥，避免继承系统里的 API_KEY（中转站 sk-xxx）
$env:API_KEY = if ($env:GAOLENG_API_KEY) { $env:GAOLENG_API_KEY } else { "gaoleng-local" }
$env:API_MODEL_NAME = if ($env:GAOLENG_MODEL_NAME) { $env:GAOLENG_MODEL_NAME } else { "gaoleng-qwen3b" }

$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Cli = Join-Path $Root ".venv\Scripts\llamafactory-cli.exe"
if (-not (Test-Path $Py)) { throw "找不到 MirrorFlow/.venv，请先按文档创建虚拟环境" }
if (-not (Test-Path (Join-Path $Root "gaoleng_api.yaml"))) {
  throw "找不到 gaoleng_api.yaml"
}
if (-not (Test-Path "E:\LLM\outputs\gaoleng-3b-lora\adapter_model.safetensors")) {
  throw "找不到 LoRA 权重 E:\LLM\outputs\gaoleng-3b-lora"
}

Write-Host "Starting gaoleng API on http://$($env:API_HOST):$($env:API_PORT)/v1"
Write-Host "Docs: http://$($env:API_HOST):$($env:API_PORT)/docs"
& $Cli api gaoleng_api.yaml
