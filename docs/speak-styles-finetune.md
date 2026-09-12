# QQ 聊天记录微调语言风格 → 学姐多风格切换

目标：用 **QQ 私聊/群记录** 做真微调，得到多种「说话风格」模型，在学姐控制台一键切换，并显示当前使用哪一种。

## 架构（分工）

| 阶段 | 在哪做 | 产出 |
|------|--------|------|
| 导出 QQ 记录 | 手机/PC QQ 或第三方导出工具 | 纯文本/JSON 对话 |
| 清洗 + LoRA 微调 | [MirrorFlow](https://github.com/qqqqqf-q/MirrorFlow)（含 QQ 路线）或 [WeClone](https://github.com/xming521/weclone) + LLaMA-Factory | 微调权重 / 部署好的 OpenAI 兼容模型 id |
| 登记 & 切换 | 学姐控制台「语言风格」 | `data/dl_senpai/speak_styles/styles.json` |
| 推理 | NoneBot `dl_senpai` | `active_model/base_url` 跟随当前风格；system 可附带 `system_overlay` |

学姐仓库**不内嵌训练**，只做「风格档案 + 路由」。

## 推荐流程（QQ）

本仓库已内置目录：`MirrorFlow/`（独立 venv：`MirrorFlow/.venv`，已 gitignore）。

```powershell
cd e:\PROJECT\QQBot\MirrorFlow
.\.venv\Scripts\Activate.ps1
```

1. **导出**与某人的聊天记录（推荐 [qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter) → JSON）。
2. **转成 MirrorFlow CSV**（跳过官方「解密 QQ.db」）：
   ```powershell
   # 在 QQBot 根目录（可用系统 python 或任意环境）
   python scripts\qce_json_to_mirrorflow_csv.py "F:\QQChatExporter\exports\friend_xxx.json" `
     --ai-uin 对方QQ号 `
     -o MirrorFlow\runs\chat\person_a\csv\chat.csv
   ```
   `--ai-uin` = **要学谁说话**的 QQ（对方），不是你自己。同时把 `MirrorFlow\setting.jsonc` 里 `qq_number_ai` 改成同一个号。
3. 清洗：
   ```powershell
   cd MirrorFlow
   .\.venv\Scripts\Activate.ps1
   python cli.py data clean raw --input runs/chat/person_a/csv --run-id person_a
   ```
4. **8GB 显存（4060 Laptop）**：用 `Qwen2.5-3B-Instruct` + QLoRA（4bit）；不要直接上 7B。
5. 下载模型并微调（需再装训练依赖，见下方）：
   ```powershell
   $env:MODELSCOPE_CACHE='E:\LLM\modelscope_cache'
   python cli.py model download --model-repo Qwen/Qwen2.5-3B-Instruct --model-path E:/LLM/models/Qwen2.5-3B-Instruct --download-source modelscope
   # 或: llamafactory-cli train gaoleng_lora.yaml
   ```
6. 部署成 OpenAI 兼容端点 → 学姐控制台 **语言风格** 登记并启用。

### 部署「高冷女神」本地推理（已训完）

```powershell
# 启动 API（默认 127.0.0.1:8001，model=gaoleng-qwen3b，key=gaoleng-local）
powershell -ExecutionPolicy Bypass -File e:\PROJECT\QQBot\MirrorFlow\start_gaoleng_api.ps1
```

风格档案：`data/dl_senpai/speak_styles/styles.json` 中的 `gaoleng_ft`。  
控制台「语言风格」可切换；或直接改 `active_id`。切到 `gaoleng_ft` 后，学姐下一轮 QQ 回复会走本地微调模型。

注意：用微调风格时需保持上述 API 进程在跑；切回「默认学姐」则仍走 `.env` 全局模型。

训练依赖（已装好时可跳过）：
```powershell
pip install -U llamafactory bitsandbytes
# 必须用 CUDA 版 torch，且不要被后续 pip 覆盖：
pip install --force-reinstall torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0+cu124 --index-url https://download.pytorch.org/whl/cu124
```

当前「高冷女神」实验产物：
- CSV：`MirrorFlow/runs/chat/gaoleng/csv/chat.csv`
- 训练集：`MirrorFlow/dataset/sft.jsonl`（约 1114 条）
- 基座：`E:/LLM/models/Qwen2.5-3B-Instruct`
- 配置：`MirrorFlow/gaoleng_lora.yaml` / `MirrorFlow/gaoleng_api.yaml`
- LoRA：`E:/LLM/outputs/gaoleng-3b-lora/`
- 启动：`MirrorFlow/start_gaoleng_api.ps1`

开训（PowerShell，务必设 UTF-8）：
```powershell
cd e:\PROJECT\QQBot\MirrorFlow
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
llamafactory-cli train gaoleng_lora.yaml
```


## 控制台能力

- 列表所有风格，高亮「使用中」
- 切换激活风格
- 增删改风格（默认学姐 `senpai_default` 不可删）
- 总览卡片显示当前风格名与套数

## 合规

仅在**当事人知情同意**下使用其聊天记录做风格克隆；生产数据务必脱敏。
