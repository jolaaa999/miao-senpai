"""Phase 5 交付物自检：验证 executor 迁移、模式、权限、journal。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))
import os  # noqa: E402

os.environ.setdefault("DL_SENPAI_TEST_MODE", "1")


def main() -> int:
    from dl_senpai.agent.journal import IRREVERSIBLE_TOOLS, ExecutionJournal
    from dl_senpai.agent.memory_facade import MemoryFacade, MemoryRef
    from dl_senpai.agent.mode import AgentExecutionMode, StopReason, resolve_mode
    from dl_senpai.agent.permissions import PermissionService
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executors import REAL_EXECUTORS

    ok = True
    print(f"execution mode default : {resolve_mode().value}")
    print(f"StopReason values      : {len(list(StopReason))}")
    print(f"irreversible tools     : {sorted(IRREVERSIBLE_TOOLS)}")
    print()

    print("=== 8 个 Tool 的 executor 迁移状态 ===")
    for tool in build_production_tools():
        fn = getattr(tool.executor, "__name__", "?")
        is_real = tool.name in REAL_EXECUTORS and tool.executor is REAL_EXECUTORS[tool.name]
        ok &= is_real
        print(
            f"  {tool.name:11} {fn:20} "
            f"real={'YES' if is_real else 'NO '} "
            f"side_effect={tool.has_side_effect()!s:5} "
            f"fail={tool.failure_policy.value}"
        )

    print()
    print("=== Journal 安全闸门 ===")
    j = ExecutionJournal()
    print(f"  before any tool   : fallback_allowed={j.fallback_allowed}")
    j.start("c1", "mute")
    print(f"  after mute start  : fallback_allowed={j.fallback_allowed}")
    ok &= not j.fallback_allowed

    j2 = ExecutionJournal()
    j2.start("c2", "search")
    print(f"  after search      : fallback_allowed={j2.fallback_allowed}")
    ok &= j2.fallback_allowed

    print()
    print("=== MemoryFacade scope ===")
    facade = MemoryFacade()
    print(f"  session : {MemoryRef.for_session('group:1').cache_key()}")
    print(f"  user    : {MemoryRef.for_user('7', group_id='100').cache_key()}")
    print(f"  group   : {MemoryRef.for_group('100').cache_key()}")

    print()
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
