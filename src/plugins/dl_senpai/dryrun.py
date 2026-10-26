r"""干跑：整条链路走一遍，只打印，不发出去。

搬自 ai-companion-kit 的 `node src/index.js --dry-run`，以及它 README 里
那句提醒——值得原样抄过来：

    ⚠️ 先干跑一遍，确认语气对了再开真发。

为什么要这个：

    调人设 / 长度 / 记忆这类改动，验证成本最高的地方是「只能在群里真试」。
    试错了要撤回、要解释，还占当天的发送配额（而且现在有熔断和限流）。
    干跑把「模型输出 + 分条结果 + 打字时长」一次性打在终端上，
    连问十句也不会有任何一条真的发出去。

用法（在仓库根目录）：

    python scripts\dryrun.py "在吗"
    python scripts\dryrun.py --private --name 老王 "我想问个事"
    python scripts\dryrun.py                     # 进入交互，逐句问，按 Ctrl+C 退出

它**不做**的事：不写记忆、不写日记、不发消息、不加好感、不碰最近聊天流水。
只读配置和已有的记忆，保证「问一百遍，线上状态一个字都不变」。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — 允许在没有 nonebot 的环境里跑

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]


@dataclass
class DryRunResult:
    """一次干跑的全部产物，方便测试断言，也方便 CLI 打印。"""

    reply: str = ""
    bubbles: list[str] = field(default_factory=list)
    typing_ms: list[int] = field(default_factory=list)
    sticker: str = ""
    voice: bool = False
    searched: bool = False
    prompt: str = ""
    error: str = ""


def _build_briefs(
    cfg: Any, user_id: str, question: str, *, group_id: str = "", private: bool = False
) -> dict[str, str]:
    """按线上同一条链路拼各段简报。

    为什么直接复用这几个 build_* 函数：干跑的意义在于「跟线上等价」，
    自己另写一份拼法，验出来的就不是线上那个人了。

    「最近聊过什么」必须带上：初版只喂了记忆和日记，结果问一句「在吗」，
    她对着空气回「有什么需要帮忙的？」——因为线上她眼前有整段群聊流水，
    干跑里却空白一片。没有上下文的人设测试，测不出真问题。
    """
    extra: dict[str, str] = {}

    # 长时印象：优先用真实存在的账本，没有就空着（干跑不该凭空造数据）
    try:
        from .person_memory import build_person_memory_brief, get_person_memory_store

        store = get_person_memory_store(getattr(cfg, "person_memory_dir", None))
        profile = store.get(user_id)
        if profile.has_content():
            brief = build_person_memory_brief(profile, query=question)
            if brief:
                extra["person_memory_brief"] = brief
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 记忆读取失败（忽略）：{exc}")

    # 她自己最近写的日记
    try:
        from .diary import build_diary_brief, get_diary_store

        if bool(getattr(cfg, "diary_enable", False)):
            store = get_diary_store(getattr(cfg, "diary_dir", None))
            brief = build_diary_brief(store, n=int(getattr(cfg, "diary_context_n", 3)))
            if brief:
                extra["diary_brief"] = brief
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 日记读取失败（忽略）：{exc}")

    # 长度纪律。干跑的价值有一半在这里：能立刻看出这条规则压不压得住
    try:
        from .brevity import build_brevity_hint

        if bool(getattr(cfg, "reply_brevity_enable", True)):
            hint = build_brevity_hint(True)
            if hint:
                extra["brevity_hint"] = hint
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 长度纪律装载失败（忽略）：{exc}")

    # 本轮语气判断：跟线上走同一条逻辑，否则干跑测出来的语气会偏离真实链路
    try:
        from .tone import build_casual_hint, build_tone_hint, classify_tone, is_casual_prompt

        if bool(getattr(cfg, "tone_classify_enable", True)):
            parts: list[str] = []
            verdict = classify_tone(question)
            hint = build_tone_hint(verdict)
            if hint:
                parts.append(hint)
            # 梗密度：与线上同一套定档逻辑，干跑才测得准
            try:
                from .meme import build_meme_hint, resolve_level

                level = resolve_level(
                    getattr(cfg, "meme_level", "auto"),
                    verdict=verdict,
                    casual=is_casual_prompt(question),
                    user_text=question,
                )
                meme = build_meme_hint(level)
                if meme:
                    parts.append(meme)
            except Exception:  # noqa: BLE001
                pass
            if is_casual_prompt(question):
                casual = build_casual_hint(
                    int(getattr(cfg, "casual_reply_hint_chars", 30) or 0)
                )
                if casual:
                    parts.append(casual)
            if parts:
                extra["tone_hint"] = "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 语气判定失败（忽略）：{exc}")

    # 群里的聊天流水：她不参与、但看在眼里的那部分
    session_id = f"private:{user_id}" if private else f"group:{group_id or '0'}"
    try:
        from .recent_chat import build_recent_chat_brief, get_recent_chat_store

        rc_store = get_recent_chat_store(getattr(cfg, "recent_chat_dir", None))
        entries = rc_store.entries(session_id)
        if entries:
            brief = build_recent_chat_brief(
                entries,
                question,
                recent_n=int(getattr(cfg, "recent_chat_recent_n", 20)),
                recall_n=int(getattr(cfg, "recent_chat_recall_n", 10)),
                max_chars=int(getattr(cfg, "recent_chat_max_chars", 2000)),
                is_private=private,
            )
            if brief:
                extra["recent_chat_brief"] = brief
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 聊天流水读取失败（忽略）：{exc}")

    # 跨场合对话流：她在别处接过的轮次（换群不失忆的补丁）
    try:
        from .cross_chat import build_cross_brief

        cross = build_cross_brief(
            current_where=session_id,
            n=int(getattr(cfg, "cross_chat_recent_n", 8) or 8),
            max_chars=int(getattr(cfg, "cross_chat_max_chars", 800) or 800),
            directory=getattr(cfg, "cross_chat_dir", None),
        )
        if cross:
            extra["cross_chat_brief"] = cross
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 跨场合流水读取失败（忽略）：{exc}")

    # 群习惯用语：只对群聊有意义
    if not private and group_id:
        try:
            from .group_style import build_group_style_brief, get_group_style_store

            store = get_group_style_store(getattr(cfg, "group_style_dir", "data/dl_senpai"))
            brief = build_group_style_brief(
                store,
                group_id,
                top_n=int(getattr(cfg, "group_style_top_n", 8)),
                min_count=int(getattr(cfg, "group_style_min_count", 3)),
            )
            if brief:
                extra["group_style_brief"] = brief
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dryrun: 群风格读取失败（忽略）：{exc}")

    # 状态栏：跟线上同一个实现。
    # 之前空跑没接这段，于是「干跑测出来的语气跟线上不一样」——
    # 状态栏里有场景/说话人，还挂着那句「你就在这个群里，直接说自己的反应」，
    # 少了它，空跑测出来的「交稿率」会偏高。
    try:
        from types import SimpleNamespace

        from .legacy_bridge import _build_status_bar

        # 线上传的是真实 ctx；空跑没有，就照它读的字段造一个最小的
        pseudo_ctx = SimpleNamespace(
            is_private=private,
            actor=user_id or "群友",
            raw_event=None,
            session_id=f"private:{user_id}" if private else f"group:{group_id}",
        )
        status = _build_status_bar(pseudo_ctx, extra)
        if status:
            extra["agent_status"] = status
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dryrun: 状态栏拼装失败（忽略）：{exc}")

    return extra


def _build_system_prompt(cfg: Any, extra: dict[str, str], *, private: bool) -> str:
    """用 PromptBuilder 拼系统提示词，与 AgentRuntime 走同一个实现。"""
    from .agent.prompt.builder import PromptBuilder, PromptRequest

    request = PromptRequest(
        private_chat=private,
        person_memory_brief=extra.get("person_memory_brief", ""),
        diary_brief=extra.get("diary_brief", ""),
        brevity_hint=extra.get("brevity_hint", ""),
        tone_hint=extra.get("tone_hint", ""),
        recent_chat_brief=extra.get("recent_chat_brief", ""),
        cross_chat_brief=extra.get("cross_chat_brief", ""),
        group_style_brief=extra.get("group_style_brief", ""),
        # few-shot 必须跟线上一致。不带的话模型没有「怎么说话」的样例，
        # 掉回助手腔——实测问一句「在吗」，它回「有什么需要我帮忙的？」，
        # 正好是人设【绝对不要说的】那一条。
        fewshot_enable=bool(getattr(cfg, "fewshot_enable", True)),
    )
    return PromptBuilder().build(request).text


def _estimate_typing_ms(cfg: Any, bubbles: list[str]) -> list[int]:
    """估算每条气泡的「打字时长」。

    这里**不做随机**：干跑要的是「这条改动让节奏变成什么样」，
    带 jitter 的数字每次都不一样，看不出变化。随机留给线上。
    """
    from .reply_send import _humanize_pause_ms

    per_char = int(getattr(cfg, "typing_ms_per_char", 0) or 0)
    hard_max = int(getattr(cfg, "typing_ms_max", 0) or 0)
    lo = int(getattr(cfg, "typing_delay_ms_min", 400) or 0)
    hi = int(getattr(cfg, "typing_delay_ms_max", 1200) or 0)
    return [
        _humanize_pause_ms(
            lo, hi, humanize_extra=False, chars=len(b), ms_per_char=per_char, hard_max=hard_max
        )
        for b in bubbles
    ]


async def run_once(
    question: str,
    *,
    sender: str = "群友",
    user_id: str = "10000",
    group_id: str = "",
    private: bool = False,
    history: list[dict[str, str]] | None = None,
    show_prompt: bool = False,
    search: bool | None = None,
) -> DryRunResult:
    """跑一次干跑。history 传入列表即可做多轮（原地追加，便于交互模式）。"""
    from .config import get_config

    cfg = get_config()
    result = DryRunResult()

    extra = _build_briefs(cfg, user_id, question, group_id=group_id, private=private)
    system_prompt = _build_system_prompt(cfg, extra, private=private)
    if show_prompt:
        result.prompt = system_prompt

    from .llm import get_llm

    llm = get_llm(cfg)
    try:
        chat_result = await llm.chat(
            history or [],
            question,
            sender_name=sender,
            private_chat=private,
            person_memory_brief=extra.get("person_memory_brief", ""),
            # diary / brevity 已经在 system_prompt_override 里了，不能再传：
            # chat() 没有 diary_brief 这个参数，传了直接 TypeError。
            system_prompt_override=system_prompt,
            # 状态栏跟线上一样作为最后一条 user 消息送进去
            status_note=extra.get("agent_status", ""),
            search_enable=search,
        )
    except Exception as exc:  # noqa: BLE001 — 干跑失败不该把终端炸掉
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.reply = (getattr(chat_result, "text", "") or "").strip()
    result.sticker = getattr(chat_result, "sticker_query", "") or ""
    result.voice = bool(getattr(chat_result, "voice_force", False))
    result.searched = bool(getattr(chat_result, "searched", False))

    from .reply_send import split_human_bubbles

    if result.reply:
        result.bubbles = split_human_bubbles(
            result.reply,
            min_chars=int(getattr(cfg, "bubble_min_chars", 40) or 0),
            prefer_chars=int(getattr(cfg, "bubble_prefer_chars", 120) or 0),
            max_bubbles=int(getattr(cfg, "bubble_max", 1) or 1),
        )
        result.typing_ms = _estimate_typing_ms(cfg, result.bubbles)

    if history is not None and result.reply:
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": result.reply})
    return result


def format_result(result: DryRunResult, *, index: int = 0) -> str:
    """把结果排成终端里好读的样子。"""
    lines: list[str] = []
    if result.error:
        lines.append(f"  ❌ 调用失败：{result.error}")
        return "\n".join(lines)

    text = result.reply or "（她这次什么都没说）"
    total = len(result.reply)
    gaps = " · ".join(f"{ms}ms" for ms in result.typing_ms) or "-"
    lines.append(f"  正文（{total} 字，{len(result.bubbles)} 条气泡）")
    for i, bubble in enumerate(result.bubbles or [text], 1):
        prefix = "   " if len(result.bubbles) <= 1 else f"   [{i}] "
        lines.append(f"{prefix}{bubble}")
    lines.append(f"  打字节奏：{gaps}")
    if result.sticker:
        lines.append(f"  会带表情包：{result.sticker}")
    if result.voice:
        lines.append("  会发语音条")
    if result.searched:
        lines.append("  （本轮触发了联网检索）")
    return "\n".join(lines)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dryrun",
        description="干跑学姐的回复：只打印，不发出去",
    )
    parser.add_argument("question", nargs="*", help="要问的话；不填则进入交互模式")
    parser.add_argument("--name", default="群友", help="对方昵称（默认「群友」）")
    parser.add_argument("--id", default="10000", help="对方 QQ 号，用来读那本记忆账")
    parser.add_argument(
        "--group", default="", help="群号：用来读这个群的聊天流水和常用语（强烈建议填）"
    )
    parser.add_argument("--private", action="store_true", help="按私聊场景拼提示词")
    parser.add_argument("--prompt", action="store_true", help="同时打印完整的系统提示词")
    parser.add_argument(
        "--search",
        choices=["on", "off", "config"],
        default="config",
        help="是否允许联网检索（默认跟配置走）",
    )
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    args = _make_parser().parse_args(argv)
    search = {"on": True, "off": False, "config": None}[args.search]
    history: list[dict[str, str]] = []

    print("════ 干跑 · 不会发出任何消息 ════")
    if args.prompt:
        print("\n---- 系统提示词 ----")
        # 提示词只跟配置有关，问同一句就能拿到；单独打一次，别每轮重复
        from .config import get_config

        cfg = get_config()
        extra = _build_briefs(
            cfg,
            args.id,
            " ".join(args.question) or "在吗",
            group_id=args.group,
            private=args.private,
        )
        print(_build_system_prompt(cfg, extra, private=args.private))
        print("---- 提示词结束 ----\n")

    questions = [" ".join(args.question)] if args.question else []
    if questions:
        for q in questions:
            if not q.strip():
                continue
            print(f"\n你：{q}")
            result = await run_once(
                q,
                sender=args.name,
                user_id=args.id,
                group_id=args.group,
                private=args.private,
                history=history,
                search=search,
            )
            print(format_result(result))
        return 0

    # 交互模式：逐句问，保持多轮上下文，Ctrl+C / EOF 退出
    print("交互模式：直接输入，回车发送。Ctrl+C 退出。\n")
    while True:
        try:
            line = input("你：")
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not line.strip():
            continue
        result = await run_once(
            line,
            sender=args.name,
            user_id=args.id,
            group_id=args.group,
            private=args.private,
            history=history,
            search=search,
        )
        print(format_result(result))
        print()


async def _aclose_llm() -> None:
    """关掉底层 httpx 连接池。

    不关的话，asyncio.run 收尾时由 GC 去回收还开着的流，终端会冒出
    「an error occurred during closing of asynchronous generator」——
    干跑明明已经成功，这段噪音却长得像失败，最容易误导人。
    """
    try:
        from .llm import get_llm

        client, _, _ = get_llm()._resolve_client()
    except Exception:  # noqa: BLE001 — 拿不到就算了，收尾不能反过来报错
        return
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001
        pass


def _silence_asyncgen_noise() -> None:
    """吞掉 httpx 流收尾时的报错噪音。

    chat() 对 deepseek-v4.1 系列走流式（见 llm._prefer_stream），
    asyncio.run 结束时这些流由 GC 回收，httpcore 会抛
    「generator didn't stop after athrow()」。**跟本次结果无关**，
    但终端上红一大片 traceback，看着像干跑失败了，最容易误导人。

    只在干跑里做这个过滤：线上进程常驻，不会走 asyncio.run 收尾那条路。
    """
    prev = sys.unraisablehook

    def _hook(unraisable: Any) -> None:
        text = repr(getattr(unraisable, "exc_value", "")) + repr(
            getattr(unraisable, "object", "")
        )
        if "generator didn't stop after athrow" in text or "safe_async_iterate" in text:
            return
        prev(unraisable)

    sys.unraisablehook = _hook


def _is_asyncgen_noise(ctx: dict[str, Any]) -> bool:
    """判断这条 loop 异常是不是「httpx 流没关干净」那种噪音。"""
    blob = " ".join(
        str(ctx.get(key, "")) for key in ("message", "exception", "asyncgen")
    )
    return "asynchronous generator" in blob or "athrow" in blob


def main(argv: list[str] | None = None) -> int:
    _silence_asyncgen_noise()

    # 自己建 loop 而不是 asyncio.run()：后者内部创建的 loop 没法挂自定义
    # 异常处理器，而「asynchronous generator」那类报错是走 loop 的
    # call_exception_handler 打到 stderr 的——只有接管 loop 才堵得住。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    fallback = loop.get_exception_handler() or loop.default_exception_handler

    def _handler(_loop: Any, ctx: dict[str, Any]) -> None:
        if _is_asyncgen_noise(ctx):
            return
        fallback(_loop, ctx)

    loop.set_exception_handler(_handler)

    async def _wrapped() -> int:
        try:
            return await _amain(argv)
        finally:
            await _aclose_llm()

    try:
        return loop.run_until_complete(_wrapped())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001 — 收尾失败不影响退出码
            pass
        loop.close()


if __name__ == "__main__":  # pragma: no cover — 只作为脚本入口
    sys.exit(main())
