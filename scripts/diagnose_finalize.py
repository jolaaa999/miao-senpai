"""诊断：_finalize_result 委托共享 adapter 后的实际行为。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))
os.environ.setdefault("DL_SENPAI_TEST_MODE", "1")


def main() -> int:
    from dl_senpai.agent.tools.marker_adapter import MarkerToolAdapter
    from dl_senpai.agent.tools.registry import get_tool_registry
    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM
    from dl_senpai.observability.retry import NoWaitRetryPolicy

    registry = get_tool_registry()
    print("registry size:", len(registry))
    print("tools:", registry.names())
    print("parseable:", [t.name for t in registry.parseable_tools()])
    print()

    adapter = MarkerToolAdapter(registry)
    text = '好\n<<<CARD {"user_id":123,"card":"小鱼干","kind":"request"}>>>'
    out = adapter.parse_response(text)
    print("direct adapter parse:")
    print("  text  :", repr(out.text))
    print("  calls :", [(c.tool_name, c.arguments) for c in out.calls])
    print()

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="m",
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())
    r = llm._finalize_result(text, card_enable=True, mute_enable=False, searched=False)
    print("_finalize_result:")
    print("  text  :", repr(r.text))
    print("  cards :", len(r.card_actions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
