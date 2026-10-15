"""验证：注册表驱动的 Prompt 与旧 if 分支产出**逐字一致**。

这是 Phase 4 最关键的等价性证明。若此测试失败，说明 prompt 文本资产
被改动了——那是不可接受的。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))


def main() -> int:
    from dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.persona import build_system_prompt

    tools = {t.name: t for t in build_production_tools()}
    builder = PromptBuilder()

    cases = [
        ("纯聊天", dict(card_enable=False, mute_enable=False, person_memory_enable=False,
                     search_enable=False, draw_enable=False, browser_enable=False), []),
        ("CARD", dict(card_enable=True, mute_enable=False, person_memory_enable=False,
                    search_enable=False, draw_enable=False, browser_enable=False), ["card"]),
        ("MUTE", dict(card_enable=False, mute_enable=True, person_memory_enable=False,
                    search_enable=False, draw_enable=False, browser_enable=False), ["mute"]),
        ("SEARCH", dict(card_enable=False, mute_enable=False, person_memory_enable=False,
                      search_enable=True, draw_enable=False, browser_enable=False), ["search"]),
        ("DRAW", dict(card_enable=False, mute_enable=False, person_memory_enable=False,
                    search_enable=False, draw_enable=True, browser_enable=False), ["draw"]),
        ("BROWSE", dict(card_enable=False, mute_enable=False, person_memory_enable=False,
                      search_enable=False, draw_enable=False, browser_enable=True), ["browse"]),
        ("全部", dict(card_enable=True, mute_enable=True, person_memory_enable=True,
                    search_enable=True, draw_enable=True, browser_enable=True),
         ["card", "mute", "impression", "search", "draw", "browse", "voice", "sticker"]),
    ]

    ok = True
    print("=== Prompt 等价性验证（registry-driven vs legacy if-branches）===\n")
    for label, flags, tool_names in cases:
        for interrupt in (False, True):
            for private in (False, True):
                for images in (False, True):
                    legacy = build_system_prompt(
                        interrupt=interrupt, has_images=images, private_chat=private,
                        fewshot_enable=False, **flags,
                    )
                    req = PromptRequest(
                        interrupt=interrupt, has_images=images, private_chat=private,
                        fewshot_enable=False,
                        visible_tools=[tools[n] for n in tool_names],
                    )
                    built = builder.build(req)
                    same = built.text == legacy
                    if not same:
                        ok = False
                        print(f"  MISMATCH {label} interrupt={interrupt} private={private} images={images}")
                        print(f"    legacy len={len(legacy)} new len={len(built.text)}")
                        for i, (a, b) in enumerate(zip(legacy, built.text)):
                            if a != b:
                                print(f"    首个差异 @{i}: legacy={a!r} new={b!r}")
                                print(f"      legacy: ...{legacy[max(0,i-40):i+40]!r}")
                                print(f"      new   : ...{built.text[max(0,i-40):i+40]!r}")
                                break
    print(f"总用例: {len(cases) * 8}")
    print("结果:", "ALL IDENTICAL" if ok else "HAS MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
