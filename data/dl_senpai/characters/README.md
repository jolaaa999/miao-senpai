# 角色档案：可信度与升级流程

## 为什么有 verified

生图提示词里写「权威设定，不得违背」是有代价的：**写错就越稳定地画错**。
所以每个视觉特征（hair / eyes / horns / clothing …）都带自己的 `verified`：

| verified | Prompt 里的档位 | 措辞 |
|---|---|---|
| true | Level A | 「已验证的身份约束，必须保留，不得违背」 |
| false | Level B | 「支撑性提示，与已验证约束/参考图冲突时以它们为准」 |
| 空值 | Level C | 完全不写（**未知 ≠ 没有**） |

`profile_status` 三档语义：

- `draft`：几乎没有人工核验 → 全部走 supporting hints
- `partial_verified`：至少部分核心字段人工确认
- `verified`：核心字段 + 参考图都已核验

## 怎么升级

1. 拿官方设定集 / 官方立绘核对某一条，例如「角：一对弯曲的羊角，深色带纹理」；
2. 把该字段改成 `{"value": "...", "verified": true}`；
3. 至少 face / hair / horns / eyes / clothing 里核验到 4 项，再把 `profile_status` 改成 `verified`；
4. 同时往 `data/dl_senpai/character_references/<作品>/<角色>/` 放官方立绘并写 `manifest.json`（字段 `verified: true`）。

## 禁止

- 不要把 AI 生成结果的视觉描述抄回这里当设定（生成结果只能用于质量评估）；
- 不要把「不知道」写成 `{"value": "无", "verified": true}`；
- 不要未核对就把 `profile_status` 改成 `verified`。
