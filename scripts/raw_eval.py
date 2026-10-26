import asyncio
import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from plugins.dl_senpai.config import get_config
from plugins.dl_senpai.dryrun import run_once
from plugins.dl_senpai.humanize import humanize_metrics

CASES = [
    "\u597d\u56f0\u554a",
    "\u4eca\u5929\u5929\u6c14\u771f\u597d",
    "\u521a\u5403\u4e86\u4e2a\u5de8\u96be\u5403\u7684\u98df\u5802",
    "\u53c8\u4e0b\u96e8\u4e86\uff0c\u70e6",
    "\u5728\u5417",
    "\u665a\u4e0a\u5403\u5565",
]
N = 4


async def main() -> None:
    cfg = get_config()
    # 关掉清洗，看模型**原本**想说什么——清洗只能捞回一半，
    # 量根因必须看未加工的输出
    cfg.humanize_enable = False
    replies = []
    for case in CASES:
        for _ in range(N):
            res = await run_once(case, sender="\u7fa4\u53cb", group_id="980229149", search=False)
            text = (res.reply or "").strip()
            if text:
                replies.append(text)
    report = humanize_metrics(replies)
    print(json.dumps(report, ensure_ascii=False))
    print("--- raw replies ---")
    for text in replies:
        print(text)


asyncio.run(main())
