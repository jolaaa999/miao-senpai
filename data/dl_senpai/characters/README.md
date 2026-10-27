# 角色档案：可信状态与升级流程

## 为什么不是简单的 verified: true/false

「被模型核对过」和「被人核验过」是两件事，混成一个布尔值会直接污染生图：

```
assistant_review → verified=true → 进了提示词的硬约束段 → 资料错了就越稳定地画错
```

所以每个视觉特征带**三态**状态：

| verification_status | 含义 | 提示词层级 | 允许的措辞 |
|---|---|---|---|
| `human_verified` | 人看过原始资料并确认 | Level A | 「不得违背 / 必须保留」 |
| `assistant_reviewed` | 模型据训练知识核对过，没人确认 | Level C | 只能说「支撑性提示，非权威」 |
| `unverified` | 没核验、没审阅 | Level D | 不写入（未知 ≠ 没有） |

代码里 `field.verified` 仍然可用，但它是**派生属性**：
`verified == (verification_status == human_verified)`。写不进去。

## 谁来写状态

只有一个入口：`scripts/character_verify.py`。

```powershell
# 看还差什么（人工交接面板）
python scripts/character_verify.py pending 艾雅法拉

# 模型先审一遍（assistant_reviewed）
python scripts/character_verify.py review-field 艾雅法拉 hair

# 人工确认（human_verified）—— 必须写 --method human_review
python scripts/character_verify.py verify-field 艾雅法拉 hair --method human_review

# 核验错了：撤销（描述保留）
python scripts/character_verify.py unverify-field 艾雅法拉 hair

# 内容有错：先改，改完自动变 unverified
python scripts/character_verify.py edit-field 艾雅法拉 hair "银白偏浅紫的长发"
```

程序、视觉模型、生图结果、后台脚本**都不能**写出 `human_verified`。
`verify-field --method assistant_review` 会被拒绝并报
`assistant_review cannot create a verified field`。

## profile_status 四档（全部自动推导）

```
必核 = hair + clothing + palette + 该角色确有值的族属特征（horns/ears/body_features）
       （不足 4 项时用 eyes/accessories/silhouette 里真有值的补足）

必核字段全部 human_verified      → verified
有 human_verified 但必核没全中   → partial_verified
只有 assistant_reviewed         → assistant_reviewed
什么都没有                      → draft
```

显式写 `draft` 仍有效（保守方向的人工锁定）；反方向升级一律看字段。

**没有任何角色特判。** 凯尔希的族属特征槽落在 `ears`，艾雅法拉落在 `horns`，
规则是同一份。

## 两条不可逆规则

1. **改内容 = 核验失效。** `edit-field` 之后状态回到 `unverified`，
   新内容不许继承旧内容的核验结论。
2. **`face` 不做文本核验。** 脸的身份无法用「圆脸/柔和五官」可靠表达，
   硬写反而会让模型重新设计人物。脸交给人工核验的参考图。

## 禁止

- 不要把 AI 生成结果的视觉描述抄回这里当设定；
- 不要把「不知道」写成 `{"value": "无", "verification_status": "human_verified"}`；
- 不要没核对就敲 `--method human_review`。
