# MirrorFlow（装在 QQBot 下）

路径：`e:\PROJECT\QQBot\MirrorFlow`  
虚拟环境：`MirrorFlow\.venv`（**Python 3.12**）

## 激活环境

```powershell
cd e:\PROJECT\QQBot\MirrorFlow
.\.venv\Scripts\Activate.ps1
```

## 当前状态（高冷女神）

| 项 | 路径/值 |
|----|---------|
| LoRA | `model_output/gaoleng-lora/` |
| 推理 API 配置 | `gaoleng_api.yaml` |
| 启动脚本 | `start_gaoleng_api.ps1` |
| 端点 | `http://127.0.0.1:8001/v1` |
| model | `gaoleng-qwen15b` |
| api_key | `gaoleng-local` |
| 学姐风格 id | `gaoleng_ft`（`data/dl_senpai/speak_styles/styles.json`） |

```powershell
powershell -ExecutionPolicy Bypass -File e:\PROJECT\QQBot\MirrorFlow\start_gaoleng_api.ps1
```

详细流程见：`docs/speak-styles-finetune.md`
