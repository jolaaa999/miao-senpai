# -*- coding: utf-8 -*-
"""把外部人设卡导入本项目。

为什么需要这个脚本：qq-bridge 的 roles/*.md 和本项目的卡格式不完全一样
（front-matter 键不同、正文长度差异大、有的卡没有 front-matter），
手工复制既慢又容易漏掉 id/name，最后变成「列表里两张同名卡，没人说得清谁生效」。

脚本只做搬运和体检，**不改正文一个字**：人设是内容，不是该被脚本润色的东西。

用法（默认 dry-run，只看会发生什么）：
    python -X utf8 scripts/import-personas.py --src C:/qq-bridge/roles
    python -X utf8 scripts/import-personas.py --src <dir> --apply
    python -X utf8 scripts/import-personas.py --src <dir> --apply --overwrite

导入后不用重启：人设卡按 mtime 热加载，存盘即生效。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARD_DIR = ROOT / "data" / "dl_senpai" / "personas"

# 这些不是卡：目录说明、模板、隐藏文件
_SKIP_STEMS = frozenset({"readme", "index", "template", "模板", "说明"})

# 体检用的粗筛。命中不代表一定有问题——只提示人去读一眼，不自动删改。
_AI_SMELL = (
    "作为一个AI", "作为 AI", "人工智能助手", "语言模型", "我不能", "很高兴为您",
    "希望对你有帮助", "总而言之", "首先，其次", "请注意", "综上所述",
)

_FRONT_MATTER = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.S)


def _is_card_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".md":
        return False
    stem = path.stem.lower()
    if stem.startswith(("_", ".")) or stem in _SKIP_STEMS:
        return False
    return True


def _split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """拆出 front-matter；没有就整篇当正文。

    只认 key: value 这种最朴素的写法。yaml 的嵌套/list 一律当字符串处理——
    我们只需要 id/name/groups/description 四个键，遇到复杂结构也没意义。
    """
    m = _FRONT_MATTER.match(raw)
    if not m:
        return {}, raw
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, raw[m.end():]


def _clean_groups(value: str) -> str:
    """把 [a, b] / a,b / a b 统一成空格分隔。

    本项目 front-matter 是逐行 key: value，塞 JSON 数组会让解析器当成字面量。
    """
    if not value:
        return ""
    v = value.strip().strip("[]")
    parts = [p.strip().strip("'\"") for p in re.split(r"[,、\s]+", v) if p.strip()]
    return " ".join(parts)


def _lint(front: dict[str, str], body: str, name: str) -> list[str]:
    problems: list[str] = []
    text = body.strip()
    if not text:
        problems.append("正文为空，导入后会被当成不存在的卡丢掉")
    elif len(text) < 200:
        problems.append(f"正文只有 {len(text)} 字，可能只是占位/未写完")
    if not front.get("name"):
        problems.append(f"没有 name，列表里会显示成 id「{name}」")
    hit = [w for w in _AI_SMELL if w in text]
    if hit:
        problems.append("疑似 AI 腔：" + "、".join(hit[:3]))
    if "persona.py" in text:
        problems.append("正文提到 persona.py，可能是本项目导出物而非独立人设")
    return problems


def build_card(src: Path) -> tuple[str, str, list[str]]:
    """返回 (card_id, 目标文本, 体检问题)。"""
    raw = src.read_text(encoding="utf-8")
    front, body = _split_front_matter(raw)

    card_id = (front.get("id") or src.stem).strip()
    name = (front.get("name") or card_id).strip()
    desc = (front.get("description") or "").strip()
    groups = _clean_groups(front.get("groups", ""))

    lines = ["---", f"id: {card_id}", f"name: {name}"]
    if groups:
        lines.append(f"groups: {groups}")
    if desc:
        lines.append(f"description: {desc}")
    lines.append("imported: 外部卡")
    lines.append("---")
    out = "\n".join(lines) + "\n" + body.strip() + "\n"

    return card_id, out, _lint(front, body, card_id)


def main() -> int:
    ap = argparse.ArgumentParser(description="导入外部人设卡")
    ap.add_argument("--src", required=True, help="源目录（如 C:\\qq-bridge\\roles）")
    ap.add_argument("--apply", action="store_true", help="真的写盘；不加则只预览")
    ap.add_argument("--overwrite", action="store_true", help="目标已存在时覆盖")
    args = ap.parse_args()

    src_dir = Path(args.src)
    if not src_dir.is_dir():
        print(f"源目录不存在：{src_dir}")
        return 2

    CARD_DIR.mkdir(parents=True, exist_ok=True)

    cards = sorted(p for p in src_dir.glob("*.md") if _is_card_file(p))
    if not cards:
        print(f"{src_dir} 里没有可导入的 md")
        return 1

    print(f"源目录 {src_dir}，发现 {len(cards)} 张卡 → {CARD_DIR}\n")

    wrote = skipped = warned = 0
    for src in cards:
        try:
            card_id, text, problems = build_card(src)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"✗ {src.name}: 读不出来（{type(exc).__name__}）")
            skipped += 1
            continue

        dst = CARD_DIR / f"{card_id}.md"
        exists = dst.exists()
        mark = "覆盖" if (exists and args.overwrite) else ("跳过" if exists else "新增")

        print(f"[{mark}] {src.name} → {dst.name}  ({len(text)} 字符)")
        for p in problems:
            print(f"      ⚠ {p}")
            warned += 1

        if args.apply and (not exists or args.overwrite):
            dst.write_text(text, encoding="utf-8")
            wrote += 1
        elif exists and not args.overwrite:
            skipped += 1

    print()
    if not args.apply:
        print("这是预览。确认没问题后加 --apply 真正写入。")
    else:
        print(f"写入 {wrote} 张，跳过 {skipped} 张，体检提示 {warned} 条。")
        print("不用重启，人设卡按 mtime 热加载。用 /人设 列表 就能看到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
