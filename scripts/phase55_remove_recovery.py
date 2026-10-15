"""Phase 5.5：删除 handlers.py 中的 4 段 legacy recovery 内联实现。

前置条件（已在 Phase 5.5 Step 0 完成）：
    tests/test_phase55_recovery_matrix.py 的决策矩阵证明
    RecoveryPolicy 与 legacy 行为完全等价。

本脚本只做一次手术式删除，不做其他改动。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "plugins" / "dl_senpai" / "handlers.py"

# 删除范围（1-based，含首尾）：从 "小模型常口头说「改好了」" 注释开始，
# 到 browse 补救块结束（下一行是 elapsed = ...）。
START_MARKER = "        # 小模型常口头说"
END_MARKER = "        elapsed = _time.perf_counter() - t0"

REPLACEMENT = '''        # [Phase 5.5 已删除] 4 段 recovery 内联实现（CARD/MUTE/DRAW/BROWSE）
        #
        # 原来这里是约 110 行、结构高度重复的「模型漏写 marker 就补救一轮」逻辑。
        # Phase 5.5 已把它收敛到 agent.RecoveryPolicy，由 AgentRuntime 驱动：
        #
        #     ToolDefinition.recovery_policy
        #         -> AgentRuntime._try_recovery()
        #         -> RecoveryPolicy.should_retry / build_retry_prompt / build_history
        #
        # 等价性由 tests/test_phase55_recovery_matrix.py 的决策矩阵断言：
        #     4 工具 x 2 intent x 2 enabled x 2 marker x 2 interrupt x 2 history
        #     （should_retry / retry prompt / history 构造 / max_retries 全等价）
        #
        # 注意：本函数只在 DL_SENPAI_AGENT_MODE=legacy 时被调用。
        # RUNTIME 模式下 recovery 由 AgentRuntime 负责，且计入 Guard
        # （steps / timeout / token budget），不再游离在护栏之外。
        was_searched = result.searched
'''


def main() -> int:
    text = HANDLERS.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if start_idx is None and line.startswith(START_MARKER):
            start_idx = i
        if start_idx is not None and line.startswith(END_MARKER):
            end_idx = i
            break

    if start_idx is None:
        print("FAIL: 找不到起始标记")
        return 1
    if end_idx is None:
        print("FAIL: 找不到结束标记")
        return 1
    if end_idx <= start_idx:
        print(f"FAIL: 范围异常 start={start_idx} end={end_idx}")
        return 1

    removed = end_idx - start_idx
    print(f"删除行范围: {start_idx + 1}..{end_idx} (共 {removed} 行)")

    new_lines = lines[:start_idx] + [REPLACEMENT] + lines[end_idx:]
    HANDLERS.write_text("".join(new_lines), encoding="utf-8")

    after = len(HANDLERS.read_text(encoding="utf-8").splitlines())
    print(f"handlers.py: {len(lines)} 行 -> {after} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
