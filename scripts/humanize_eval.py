"""拟人化评估：把她一次次的回复量化成「像不像人」的几个数。

来源：sakurawwwxh/qq-agent-plus 的 scripts/humanize-eval.mjs。
为什么需要它：之前调人味全靠眼看，改完觉得「好像好点了」，
下次又觉得变差了——没有数字就只能来回摇摆。
这份是**观察线**，不是硬门禁：单看某一项都能骗过自己。

用法：
    python scripts\humanize_eval.py --n 3
    python scripts\humanize_eval.py --n 3 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 挑的都是群里真实会出现的随口话：没有提问、没有任务，
# 这种最考验「像不像人」——答疑本来就该像人写文档，看不出差别。
CASES = [
    "好困啊",
    "今天天气真好",
    "刚吃了个巨难吃的食堂",
    "又下雨了，烦",
    "在吗",
    "晚上吃啥",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3, help="每条跑几遍")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    from plugins.dl_senpai.dryrun import run_once
    from plugins.dl_senpai.humanize import find_ai_tells, humanize_metrics

    replies: list[str] = []
    for case in CASES:
        for _ in range(max(1, args.n)):
            try:
                res = await run_once(
                    case, sender="群友", group_id="980229149", search=False
                )
            except Exception as exc:  # noqa: BLE001
                print(f"跳过（调用失败）：{case} :: {exc}", file=sys.stderr)
                continue
            text = (res.reply or "").strip()
            if text:
                replies.append(text)
                if not args.json:
                    print(f"{case} -> {text}")

    report = humanize_metrics(replies)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    tells: dict[str, int] = {}
    for text in replies:
        for tell in find_ai_tells(text):
            tells[tell["label"]] = tells.get(tell["label"], 0) + 1

    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if tells:
        print("命中明细：" + "，".join(f"{k}×{v}" for k, v in tells.items()))
    else:
        print("命中明细：无")
    if float(report["aiTellRate"]) > 0.05:
        print(f"提示：{float(report['aiTellRate']) * 100:.1f}% 的回复命中助手腔")
    if float(report["duplicateOpenerRate"]) > 0.2:
        print(f"提示：{float(report['duplicateOpenerRate']) * 100:.1f}% 的回复开头重复")
    # 翻案腔/清单/顿号密度：来自 natural-talk 的机械感清单，
    # 这几项高了说明她还在「写东西」而不是「说话」
    if float(report["negationFlipRate"]) > 0:
        print(f"提示：{float(report['negationFlipRate']) * 100:.0f}% 用了翻案腔（不是A，而是B）")
    if float(report["enumerationRate"]) > 0:
        print(f"提示：{float(report['enumerationRate']) * 100:.0f}% 在列清单（冒号标签 ≥2 个）")
    if float(report["dunhaoPerSentence"]) > 2:
        print(f"提示：每句叠 {report['dunhaoPerSentence']} 个顿号，并列堆得太密")


if __name__ == "__main__":
    asyncio.run(main())
