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

## manifest.json

**没有清单的图不算参考图。** 代码只认清单里 `verified: true` 的条目：

```json
{
  "character": "Eyjafjalla",
  "localized_name": "艾雅法拉",
  "franchise": "Arknights",
  "verified": true,
  "references": [
    {
      "file": "official_fullbody_01.png",
      "type": "official_fullbody",
      "verified": true,
      "priority": 100,
      "notes": "官方立绘，全身"
    }
  ]
}
```

字段说明：

| 字段 | 作用 |
|---|---|
| `verified` | 只有 true 才会被用于 `images.edit` |
| `type` | 没确认官方来源就写 `verified_reference`，不要写 `official_*` |
| `priority` | 越大越优先，多张图时按序取第一张可用 |
| `notes` | 出处、版本、注意事项 |

## 只读

这里**只读**。运行时不会把网上搜到的图写进来——那等于用未核实的素材污染库。
要收图请人工下载、核对、再补 manifest。

## 校验

```python
from plugins.dl_senpai.character_references import validate_character_profile
issues = validate_character_profile(config, profile)   # 结构性冲突清单
```
