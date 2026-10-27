# 角色参考图库（V1）

## 目录约定

```
data/dl_senpai/character_references/
  arknights/
    eyjafjalla/
      manifest.json
      official_fullbody_01.png
```

路径是 `<franchise slug>/<character slug>`，slug 由 CharacterProfile.slug 生成。

## 导入流程（只有这一条路）

```powershell
# 人工核验过的图（会用进 images.edit）
python scripts/character_reference.py add 艾雅法拉 "C:\refs\eyja_fullbody.png" `
  --role fullbody --source-type official --verified --method human_review

# 只是让模型过了一眼的图（不会用于 images.edit）
python scripts/character_reference.py add 艾雅法拉 "C:\refs\x.png" --reviewed
```

它会：读本地文件 → 校验图片头与大小 → 算 SHA256 → 拒绝重复 → 复制进库 → 写 manifest。

**不会联网下载。** `--verified` 却不写 `--method human_review` 会被直接拒绝——
"顺手加个 --verified 就把图升级成身份权威" 是上一阶段事故的同款入口。

## manifest 字段

| 字段 | 作用 |
|---|---|
| `verification_status` | `human_verified` 才会被 `images.edit` 使用 |
| `reference_role` | `identity` / `fullbody` / `portrait`；按用户要的构图挑图 |
| `priority` | 同 role 下越大越优先 |
| `sha256` | 完整性指纹，导入/核验时自动写，**不要手改** |
| `verification_method` | `human_review` 才代表人类看过原图 |
| `source_url` | 只做记录，**不决定** 状态 |

示例见 `manifest.example.json`。旧清单里的 `verified: true` 会按方法自动迁移：
`human_review` → `human_verified`，`assistant_review` → `assistant_reviewed`，
没写方法 → `unverified`（**不能靠一个裸 true 继承权威性**）。

## 完整性

读取时校验：文件存在、文件头是真图片、大小 ≤ 8MB、SHA256 与清单一致。

```powershell
python scripts/character_reference.py check 艾雅法拉
```

哈希不符的图会被拒绝，并在 trace 里记 `reference_integrity_failed`。
清单说核验过只证明「当初」核验过，不证明「现在还是那张图」。

元数据变化（role / source_type / notes）不影响核验；
`file` 或 `sha256` 变化则必须失效。

## 撤销

```powershell
python scripts/character_reference.py unverify 艾雅法拉 official_fullbody_01.png
python scripts/character_reference.py remove   艾雅法拉 official_fullbody_01.png --delete-file
```

## 版权与 Git

- 图片二进制**不进 Git**（`.gitignore` 已忽略 `character_references/**/*.png`）；
- 仓库只保留 README、`manifest.example.json` 与工具代码；
- 导入命令不会自动 `git add`。

## 只读

运行时**只读**。生图链路不会把搜到的图写进库——否则「搜到同人图 → 自动入库 →
下次当人工核验图」的自我污染迟早发生。
