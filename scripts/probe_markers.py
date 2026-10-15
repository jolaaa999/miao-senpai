"""探查 8 种 marker 的真实解析行为，用于编写行为快照测试。

不改任何代码，只读取现有实现的输入输出，作为 Phase 4 迁移的对照基准。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))
import os  # noqa: E402

os.environ.setdefault("DL_SENPAI_TEST_MODE", "1")


def show(title: str, fn, cases: list[str]) -> None:
    print(f"\n=== {title} ===")
    for text in cases:
        try:
            result = fn(text)
        except Exception as exc:  # noqa: BLE001
            print(f"  {text!r:60} -> EXC {type(exc).__name__}: {exc}")
            continue
        print(f"  {text!r:60} -> {result}")


def main() -> None:
    from dl_senpai.browser_agent import parse_browse_request
    from dl_senpai.group_card import parse_card_actions
    from dl_senpai.group_mute import parse_mute_actions
    from dl_senpai.image_gen import parse_draw_request
    from dl_senpai.person_memory import parse_impression_updates
    from dl_senpai.stickers import parse_sticker_request
    from dl_senpai.voice import parse_voice_request
    from dl_senpai.web_search import parse_search_request

    show(
        "CARD",
        lambda t: (parse_card_actions(t).text, [a.__dict__ for a in parse_card_actions(t).actions]),
        [
            '你好\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>',
            '你好\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>',
            '你好\n<<<CARD {bad json}>>>',
            '你好',
            '<<<CARD {"user_id":1,"card":"a","kind":"request"}>>>\n<<<CARD {"user_id":2,"card":"b","kind":"request"}>>>',
        ],
    )

    show(
        "MUTE",
        lambda t: (parse_mute_actions(t).text, [a.__dict__ for a in parse_mute_actions(t).actions]),
        [
            '好\n<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>>',
            '好\n<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>',
            '好\n<<<MUTE {bad}>>>',
            '好',
        ],
    )

    show(
        "DRAW",
        lambda t: (parse_draw_request(t).text, parse_draw_request(t).prompt, parse_draw_request(t).force),
        [
            "图\n<<<DRAW a cat>>>",
            "图\n<<<DRAW a cat>>",
            "图\n<<<DRAW a cat】",
            "图\n<<<DRAW>>>",
            "图",
        ],
    )

    show(
        "BROWSE",
        lambda t: (parse_browse_request(t).text, len(parse_browse_request(t).tasks), parse_browse_request(t).force),
        [
            "找\n<<<BROWSE pixiv 猫>>>",
            "找\n<<<BROWSE pixiv 猫>>",
            "找\n<<<BROWSE>>>",
            "找",
        ],
    )

    show(
        "STICKER",
        lambda t: (parse_sticker_request(t).text, parse_sticker_request(t).query, parse_sticker_request(t).force),
        [
            "哼\n<<<STICKER 傲娇>>>",
            "哼\n<<<STICKER>>>",
            "哼",
        ],
    )

    show(
        "VOICE",
        lambda t: (parse_voice_request(t).text, parse_voice_request(t).override, parse_voice_request(t).force),
        [
            "在呢\n<<<VOICE>>>",
            "在\n<<<VOICE 晚安>>>",
            "在\n<<<VOICE 晚安】",
            "在",
        ],
    )

    show(
        "IMPRESSION",
        lambda t: (parse_impression_updates(t).text, len(parse_impression_updates(t).updates)),
        [
            '<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>',
            '<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>',
            '<<<IMPRESSION {bad}>>>',
            '',
        ],
    )

    show(
        "SEARCH",
        lambda t: (parse_search_request(t).text, parse_search_request(t).queries),
        [
            "<<<SEARCH 天气>>>",
            "<<<SEARCH 天气>>",
            "<<<SEARCH 天气】",
            "<<<SEARCH 天气＞＞＞",
            "问题\n<<<SEARCH 天气>>>\n尾巴",
            "没有marker",
        ],
    )


if __name__ == "__main__":
    main()
