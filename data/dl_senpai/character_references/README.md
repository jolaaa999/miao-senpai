# 角色参考图库（V1）

## 目录约定

```
data/dl_senpai/character_references/
  arknights/
    eyjafjalla/
      manifest.json
      official_fullbody_01.png
```

路径是 `<franchise slug>/<character slug>`，slug 由 CharacterProfile.slug 生成
（全小写、非字母数字转 `-`）。

## 导入流程（只有这一条路）

```powershell
python scripts/character_reference.py add 艾雅法拉 "C:\refs\eyja_fullbody.png" `
  --role fullbody --source-type official --verified
```

它会：读本地文件 → 校验图片头与大小 → 算 SHA256 → 拒绝重复 →
复制进库 → 写进 manifest。

**不会联网下载，也不会因为域名像官网就自动 verified。** `--verified` 是人的决定。

## manifest 字段

| 字段 | 作用 |
|---|---|
| `verified` | 只有 true 才会被用于 `images.edit` |
| `reference_role` | `identity` / `fullbody` / `portrait`；按用户要的构图挑图 |
| `priority` | 同 role 下越大越优先 |
| `sha256` | 完整性指纹。导入/核验时自动写，**不要手改** |
| `verification_method` | `human_review` 才代表人类看过原图 |
| `source_url` | 只做记录，**不决定 verified** |

示例见 `manifest.example.json`。

## 完整性

读取时会校验：文件存在、文件头是真图片、大小 ≤ 8MB、SHA256 与清单一致。

```powershell
python scripts/character_reference.py check 艾雅法拉
```

哈希不符的图会被直接拒绝，并在 trace 里记 `reference_integrity_failed`。
清单说 verified 只证明「当初」核验过，不证明「现在还是那张图」。

## 撤销

```powershell
python scripts/character_reference.py unverify 艾雅法拉 official_fullbody_01.png
python scripts/character_reference.py remove   艾雅法拉 official_fullbody_01.png --delete-file
```

## 版权与 Git

角色官方立绘属于第三方版权资产：

- 图片二进制**不进 Git**（`.gitignore` 已忽略 `character_references/**/*.png`）；
- 仓库里只保留 README、`manifest.example.json` 和工具代码；
- 导入命令不会自动 `git add`。

## 只读

运行时**只读**。生图链路不会把搜到的图写进库——否则会出现
「搜到同人图 → 自动入库 → 下次当已验证参考图」的自我污染。
