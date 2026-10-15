"""Phase 6.5：把 _handle_chat_inner 的**函数体原样**追加到 legacy_orchestrator.py。

第二/七节要求：Move not Copy，且先搬不改。
做法：直接把 AST 提取的函数源码加一段缩进，形成 LegacyOrchestrator.run()。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "scripts" / "_phase65_work"
TARGET = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

HEADER = '''

# ================================================================
# Legacy 编排主体（Phase 6.5 从 handlers.py 原样搬运）
# ================================================================
#
# 下面这个函数体与搬运前 handlers._handle_chat_inner **逐字相同**，
# 只做了两处纯机械调整：
#   1. 缩进 +4（从顶层函数变成类方法）
#   2. 签名改为 (self, bot, event, *, force_mention, config)
#
# 之所以保留 736 行的完整形状而不是"顺手拆成命名步骤"：
#   Phase 6.5 第二节要求 Move first；拆分会同时引入两类变更，
#   导致 1323 个测试的失败无法归因。拆分见 Phase 7。


class LegacyOrchestrator:
    """旧 Agent 编排（第一节）。

    与 RuntimeOrchestrator 的差异**仅限 orchestration 层**：
    它自己组装 prompt、自己调 SenpaiLLM、自己走单轮流程；
    但 marker 解析、工具执行、权限、记忆、恢复全部复用共享实现。

    为什么不用 `AgentContext` 而是收 (bot, event, ...)：
    legacy 编排深度依赖 OneBot 的 Bot / MessageEvent 对象（发消息、
    取群成员、读 at 列表），而 AgentContext 是平台无关的。
    Phase 6A 已建立 `legacy_context.build_legacy_context()` 在需要时
    把 (bot, event) 转成 AgentContext 供共享 ToolExecutor 使用 ——
    这就是两者之间的桥。
    """

    async def run(
        self,
        bot: Bot,
        event: MessageEvent,
        *,
        force_mention: bool,
        config: PluginConfig,
    ) -> None:
'''


def main() -> int:
    src = (WORK / "handle_chat_inner.py.txt").read_text(encoding="utf-8")
    lines = src.splitlines()

    # 去掉 def 行与紧随的签名续行，只保留函数体
    # 签名形如：
    #   async def _handle_chat_inner(
    #       bot: Bot,
    #       ...
    #   ) -> None:
    #       <body>
    body_start = 0
    for i, line in enumerate(lines):
        if line.rstrip().endswith("-> None:"):
            body_start = i + 1
            break
    if body_start == 0:
        print("FAIL: 找不到函数体起点")
        return 1

    body = lines[body_start:]
    # 原函数体缩进 4，方法体需要 8
    indented = [
        ("    " + line) if line.strip() else line
        for line in body
    ]

    out = HEADER + "\n".join(indented) + "\n"
    text = TARGET.read_text(encoding="utf-8")
    TARGET.write_text(text + out, encoding="utf-8")

    total = len(TARGET.read_text(encoding="utf-8").splitlines())
    print(f"已追加函数体 {len(indented)} 行")
    print(f"legacy_orchestrator.py 现有 {total} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
