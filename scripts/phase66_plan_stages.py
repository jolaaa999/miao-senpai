"""Phase 6.6：把 LegacyOrchestrator.run() 拆成命名的语义阶段。

第三/九/十节要求：
    - 拆成有业务语义的阶段方法，禁止 _step1/_step2
    - run() 最终只做 orchestration
    - **移动代码，不改行为**（第二节：Preserve behavior → Extract → Move → Verify）

做法：
    1. 按 phase66_stage_map.py 确认的边界切分 run() 的源码
    2. 每段包成 `async def <stage>(self, state, ...)` 方法
    3. 段内变量名从裸局部改为 `state.<field>`（纯改名）
    4. run() 变成顺序调用

因为改名较多，本脚本生成**中间产物**供人工核对，再落盘。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

# 阶段边界（行号基于 Phase 6.6 审计输出，1-based）
# (起, 止, 方法名, 是否 async, 需要的额外参数)
STAGES: list[tuple[int, int, str, bool, list[str]]] = [
    (181, 184, "resolve_session_stage", True, ["bot", "event", "config", "force_mention"]),
    (186, 199, "prepare_message_context", True, ["bot", "event", "config", "force_mention"]),
    (200, 221, "prepare_media_context", True, ["bot", "event", "config", "force_mention"]),
]


def get_run() -> ast.AsyncFunctionDef:
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            for m in node.body:
                if isinstance(m, ast.AsyncFunctionDef) and m.name == "run":
                    return m
    raise SystemExit("找不到 run")


def main() -> int:
    fn = get_run()
    lines = LEGACY.read_text(encoding="utf-8").splitlines()
    print(f"run(): L{fn.lineno}-{fn.end_lineno}")

    for start, end, name, is_async, params in STAGES:
        seg = lines[start - 1 : end]
        print(f"\n--- {name} (L{start}-{end}, {len(seg)} 行) ---")
        print(f"    参数: {params}")
        for line in seg[:4]:
            print(f"      {line}")
        if len(seg) > 4:
            print(f"      ... ({len(seg) - 4} 行省略)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
