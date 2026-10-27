# 角色档案：可信度与升级流程

## 为什么有 verified

生图提示词里写「权威设定，不得违背」是有代价的：**写错就越稳定地画错**。
所以每个视觉特征（hair / eyes / horns / clothing …）都带自己的 `verified`：

| verified | Prompt 里的档位 | 措辞 |
|---|---|---|
| true | Level A | 「已验证的身份约束，必须保留，不得违背」 |
| false | Level A 外 | supporting hints（参考性质，冲突时以已验证项/参考图为准） |
| 空值 | Level C | 完全不写（**未知 ≠ 没有**） |

## 谁来写 verified

只有一个入口：`scripts/character_verify.py`。

程序、视觉模型、生图结果**都不能**把 `verified` 从 false 改成 true。
把生成结果学回档案，等于让模型自己给自己出题再自己判卷。

## 流程

```powershell
# 看现状
python scripts/character_verify.py show 艾雅法拉

# 补/改描述（只改文案，不动核验状态）
python scripts/character_verify.py set-value 艾雅法拉 palette "银白与浅紫为主调…"

# 人工确认一个字段
python scripts/character_verify.py verify-field 艾雅法拉 horns --method human_review

# 发现核验错了：撤销（描述保留）
python scripts/character_verify.py unverify-field 艾雅法拉 clothing --note "认错了"
```

## profile_status 不手写

状态永远由字段推导：

```
必核 = hair + clothing + palette + 该角色确实有值的族属特征（horns/ears/body_features）
       （不足 4 项时用 eyes/accessories/silhouette 里真有值的补足）
必核全中 → verified
命中任意一项 → partial_verified
一项都没有 → draft
```

`face` **不在必核里**：脸的身份基本无法用「圆脸/柔和五官」这种自然语言可靠表达，
硬编一段描述反而会让生图模型重新设计人物。脸交给已验证参考图。

显式写 `draft` 仍然有效——那是保守方向的人工锁定。反方向的升级一律看字段。

## 禁止

- 不要把 AI 生成结果的视觉描述抄回这里当设定；
- 不要把「不知道」写成 `{"value": "无", "verified": true}`；
- 不要未核对就写 `--method human_review`。
