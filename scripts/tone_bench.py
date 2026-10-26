"""语气基准：同一句话多跑几遍，数出「安慰腔」到底占多少。

为什么必须有这个脚本：

温度 1.1 方差很大，单次干跑说明不了任何问题——同一句「今天好累」
跑三次可能两次安慰、一次正常。改提示词前后各跑一次就下结论，
等于在看噪声。所以这里固定跑 N 遍，只报比例。

用法：
    python scripts\tone_bench.py              # 默认每句 3 遍
    python scripts\tone_bench.py --n 5
    python scripts\tone_bench.py --no-tone    # 关掉语气判定，作对照
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 探针故意比 tone.py 的删除词表**更宽**：
# 线上会按词表删除安慰小句，如果基准也用同一份词表，删干净了就等于自己给自己刷分。
# 这里额外加一批「不会被删掉、但读起来仍然是安慰」的说法，
# 用来衡量「删完之后还剩多少安慰味」，而不是「还有多少词命中」。
_EXTRA_PROBE_MARKERS = (
    "注意",
    "别太",
    "早睡",
    "休息",
    "照顾",
    "累了就",
    "要不先",
    "心疼",
)

# 测试语料：前 6 句是吐槽（不该安慰），后 3 句是真倾诉（该安慰）。
_VENT_CASES = (
    "今天好累啊，作业写到两点",
    "困死了，早八根本起不来",
    "服了，课设又要重做",
    "加班到现在，麻了",
    "这破作业写不完了笑死",
    "好烦啊，组会又要汇报",
)
_DISTRESS_CASES = (
    "最近一直睡不着，感觉挺难受的",
    "真的撑不住了，感觉没人在乎",
    "能陪我说说话吗，这几天特别崩溃",
)


def _has_comfort(text: str) -> bool:
    from plugins.dl_senpai.tone import has_comfort_tone

    if has_comfort_tone(text):
        return True
    return any(marker in text for marker in _EXTRA_PROBE_MARKERS)


async def _bench(n: int, use_tone: bool) -> int:
    from plugins.dl_senpai.config import get_config
    from plugins.dl_senpai.dryrun import _aclose_llm, run_once
    from plugins.dl_senpai.llm import get_llm

    cfg = get_config()
    if not use_tone:
        # 直接改运行时配置对象：基准脚本要的是「对照组」，不落盘
        object.__setattr__(cfg, "tone_classify_enable", False)

    llm = get_llm(cfg)
    rows: list[tuple[str, str, int, int]] = []

    try:
        for kind, cases in (("吐槽", _VENT_CASES), ("倾诉", _DISTRESS_CASES)):
            for question in cases:
                comfort = 0
                lengths: list[int] = []
                for _ in range(n):
                    res = await run_once(
                        question,
                        sender="群友",
                        user_id="10000",
                        group_id="980229149",
                        private=False,
                        search=False,
                    )
                    text = (res.reply or "").strip()
                    lengths.append(len(text))
                    if _has_comfort(text):
                        comfort += 1
                avg_len = sum(lengths) // max(1, len(lengths))
                rows.append((kind, question, comfort, avg_len))
    finally:
        await _aclose_llm()

    # 汇总：吐槽组的安慰率是核心指标，倾诉组的安慰率反过来是好事
    print()
    print(f"{'类型':<4} {'安慰次数':<8} {'平均字数':<8} 测试句")
    print("-" * 78)
    vent_comfort = 0
    distress_comfort = 0
    for kind, question, comfort, avg_len in rows:
        print(f"{kind:<6} {comfort}/{n:<8} {avg_len:<10} {question}")
        if kind == "吐槽":
            vent_comfort += comfort
        else:
            distress_comfort += comfort
    total_vent = n * len(_VENT_CASES)
    total_distress = n * len(_DISTRESS_CASES)
    print("-" * 78)
    print(f"吐槽组进安慰模式：{vent_comfort}/{total_vent}  ({vent_comfort * 100 // max(1, total_vent)}%)  ← 越低越好")
    print(f"倾诉组有安慰：    {distress_comfort}/{total_distress}  ({distress_comfort * 100 // max(1, total_distress)}%)  ← 越高越好")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语气基准：数安慰腔比例")
    parser.add_argument("--n", type=int, default=3, help="每句跑几遍（默认 3）")
    parser.add_argument("--no-tone", action="store_true", help="关掉本轮语气判定，作对照")
    args = parser.parse_args(argv)
    return asyncio.run(_bench(args.n, not args.no_tone))


if __name__ == "__main__":
    raise SystemExit(main())
