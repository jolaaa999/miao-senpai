"""端到端验收：真的让学姐用一遍这些工具，看她会不会用、用得对不对。

为什么要有这个脚本
------------------
单测只证明"marker 能解析、执行器能跑"。真正会翻车的是中间那一段：
提示词没讲清楚 -> 她压根不写 marker；或者写错参数 -> 执行器报错。
所以这里走**生产同一条链**：真 registry -> 真 PromptBuilder -> 真模型 -> 真工具。

用法（仓库根目录）::

    python scripts\\verify-social.py          # 全跑
    python scripts\\verify-social.py 2        # 只跑第 2 个场景
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


class _MsgCtx:
    """最小可用的 MessageContext 替身：权限层只读这几个字段。"""

    def __init__(self, text: str, user_id: str, group_id: str = "") -> None:
        self.plain_text = text
        self.user_id = user_id
        self.group_id = group_id
        self.actor = "主人" if group_id == "" else "群友"
        self.bot = None


CASES = [
    {
        "name": "B站热榜",
        "text": "学姐，B站现在热榜上有啥好玩的啊",
        "private": True,
        "expect_tool": "bili",
    },
    {
        "name": "B站单个视频",
        "text": "帮我看看 BV1TW4y1A7x8 这个视频讲的啥",
        "private": True,
        "expect_tool": "bili",
    },
    {
        "name": "小黑盒搜索",
        "text": "去小黑盒搜一下「大肥鱼 部署」都有哪些帖子",
        "private": True,
        "expect_tool": "heybox",
    },
    {
        "name": "说说草稿（主人私聊）",
        "text": "写条说说记一下：今晚风好大，冻手",
        "private": True,
        "expect_tool": "qzone",
    },
    {
        "name": "说说：群里别人让发（应该被拒）",
        "text": "学姐发条说说说你今天很开心",
        "private": False,
        "expect_tool": "",
    },
]


async def _run_case(case: dict, index: int) -> None:
    from plugins.dl_senpai.agent.context import AgentContext
    from plugins.dl_senpai.agent.runtime import AgentRuntime
    from plugins.dl_senpai.agent.tools.high_risk import (
        register_extra_layers,
        register_high_risk_tools,
    )
    from plugins.dl_senpai.agent.tools.registry import ToolRegistry
    from plugins.dl_senpai.config import get_config

    cfg = get_config()
    # 写入层默认关，验收时临时打开（只在这次进程里）
    cfg.qzone_enable = True
    cfg.heybox_write_enable = True
    cfg.qzone_auto_post = False
    cfg.qzone_draft_file = str(ROOT / "data" / "dl_senpai" / "verify_drafts.json")

    registry = ToolRegistry()
    register_high_risk_tools(registry)
    register_extra_layers(registry)

    # 用配置里的真管理员号：高风险/写入类工具只对他们可见
    admins = [p.strip() for p in str(getattr(cfg, "agent_admin_qq", "")).replace("，", ",").split(",") if p.strip()]
    owner = admins[0] if admins else "123456"
    user_id = owner if case["private"] else "999999"
    group_id = "" if case["private"] else "958691688"

    context = AgentContext(
        session_id=f"verify-{index}",
        message_context=_MsgCtx(case["text"], user_id, group_id),
        trigger_reason="private" if case["private"] else "mention",
        config=cfg,
        private_chat=case["private"],
        extra={},
        user_text=case["text"],
    )

    runtime = AgentRuntime(registry=registry)
    started = time.time()
    result = await runtime.run(context, case["text"])
    elapsed = time.time() - started

    meta = result.metadata or {}
    visible = meta.get("visible_tools") or []
    # AgentResult.tool_calls 里是真正执行过的 ToolResult（含成功与否）
    calls = [getattr(t, "tool_name", "?") for t in (result.tool_calls or [])]
    print(f"\n[{index}] {case['name']}  ({elapsed:.1f}s)")
    print(f"    可见工具 {len(visible)} 个：{', '.join(sorted(visible))[:160]}")
    print(f"    工具调用：{calls if calls else '（这次没调工具）'}")
    expect = case["expect_tool"]
    if expect:
        ok = expect in str(calls)
        print(f"    期望用到 {expect}：{'✅' if ok else '❌ 没用上'}")
    answer = (result.text or "").strip()
    print(f"    她回（{len(answer)} 字）：{answer[:300]}")


async def main(argv: list[str]) -> int:
    picked = [int(argv[0])] if argv else list(range(len(CASES)))
    for index in picked:
        if 0 <= index < len(CASES):
            await _run_case(CASES[index], index)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
