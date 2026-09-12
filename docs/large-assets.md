# 大文件下载（模型权重 / GPT-SoVITS）

因体积超过 GitHub 普通仓库与免费 LFS 额度，大文件以 **Release 分卷** 发布。

仓库：https://github.com/jolaaa999/miao-senpai  
Release 标签：`assets-v1`

## 包含内容

| 包名 | 内容 |
|------|------|
| `qwen25-3b-and-lora.7z.*` | `Qwen2.5-3B-Instruct` 基座 + `gaoleng-3b-lora` |
| `gpt-sovits-v2pro.7z.*` | `GPT-SoVITS-v2pro-20250604`（已排除 logs/TEMP） |

## 下载与解压

1. 打开 Releases，把同一前缀的所有分卷下到同一目录。  
2. 用 7-Zip 打开 `.7z.001` 解压：

```powershell
# 示例：解压到项目目录旁
& "C:\Program Files\7-Zip\7z.exe" x qwen25-3b-and-lora.7z.001 -oE:\LLM\
& "C:\Program Files\7-Zip\7z.exe" x gpt-sovits-v2pro.7z.001 -oE:\PROJECT\QQBot\GPT-SoVITS-v2pro-20250604\
```

解压后目录结构应对齐：

- `E:\LLM\models\Qwen2.5-3B-Instruct\`
- `E:\LLM\outputs\gaoleng-3b-lora\`
- `E:\PROJECT\QQBot\GPT-SoVITS-v2pro-20250604\`
