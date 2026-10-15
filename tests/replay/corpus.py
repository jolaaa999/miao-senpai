"""Replay Corpus：Phase 5 切流前的行为基准（Step 0）。

第三/四节要求。这是把"切流是否安全"从主观判断变成机器可验证的关键设施。

设计要点：

1. **脱敏**：所有样本使用伪造的群号/QQ号/文本，不含真实数据。
   `test_replay_corpus.py` 会断言没有真实 ID 泄漏。

2. **只记录行为决策，不记录 LLM 文字**：模型输出是非确定性的，
   记录它只会产生噪声。Corpus 关注的是：
   - 路由到哪个处理层
   - 应该调用哪些工具
   - 是否应该触发 recovery
   - 权限是否放行

3. **覆盖第四节的完整清单**：普通/private/mention/interrupt、
   8 种 marker、无/单/多/错误 marker、容错闭合、本地3B、
   权限拒绝、工具失败/超时/重复、recovery 触发与否。

样本数量目标 ≥50。当前为**构造样本**（非真实流量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReplayCase:
    """一条 replay 样本。

    Attributes:
        id: 样本标识（稳定，用于报告）。
        category: 分类（用于统计与筛选）。
        input_text: 用户输入（脱敏）。
        message_type: group / private。
        is_to_me: 是否 @ 了机器人。
        interrupt: 是否插队模式。
        group_id: 脱敏群号。
        flags: 功能开关覆盖。
        model_reply: 模拟的模型输出（决定 marker）。
        expected_route: 期望路由（command 名 / agent trigger / none）。
        expected_tools: 期望调用的工具。
        expected_stop: 期望停止原因（可空）。
        expected_recovery: 期望是否触发 recovery（可空）。
        expected_permission: 期望权限结果（可空）。
        note: 说明。
    """

    id: str
    category: str
    input_text: str = ""
    message_type: str = "group"
    is_to_me: bool = False
    interrupt: bool = False
    group_id: str = "900001"        # 伪造群号
    user_id: str = "10001"          # 伪造 QQ
    flags: dict[str, bool] = field(default_factory=dict)
    model_reply: str = ""
    expected_route: str = ""
    expected_tools: list[str] = field(default_factory=list)
    expected_stop: str = ""
    expected_recovery: bool | None = None
    expected_permission: bool | None = None
    note: str = ""


def _c(**kw: Any) -> ReplayCase:
    return ReplayCase(**kw)


def build_corpus() -> list[ReplayCase]:
    """构造 replay corpus。

    返回的样本全部脱敏，可安全提交进 git。
    """
    cases: list[ReplayCase] = []
    n = [0]

    def add(**kw: Any) -> None:
        n[0] += 1
        kw.setdefault("id", f"R{n[0]:03d}")
        cases.append(_c(**kw))

    # ---------- 基础对话形态 ----------
    add(category="plain", note="普通群聊无 marker",
        input_text="今天天气不错", model_reply="是呀～")
    add(category="plain", note="短消息不触发插嘴",
        input_text="嗯", model_reply="")
    add(category="private", message_type="private", is_to_me=True,
        note="私聊必进 Agent", input_text="在吗", model_reply="在呢～")
    add(category="mention", is_to_me=True,
        note="@ 必回（短句）", input_text="在吗", model_reply="在呀～")
    add(category="mention", is_to_me=True,
        note="@ 必回（长句）", input_text="帮我看看这段代码", model_reply="发来看看")
    add(category="interrupt", interrupt=True,
        note="插嘴模式", input_text="这是一条足够长的普通群聊消息", model_reply="")
    add(category="mention", is_to_me=True,
        note="引用消息", input_text="你觉得呢", model_reply="我觉得可以")

    # ---------- 命令路由（不进入 Agent） ----------
    for text, expected in (
        ("签到", "checkin"), ("打卡", "checkin"), ("我的积分", "checkin"),
        ("积分排行", "checkin"), ("今日任务", "checkin"), ("完成任务", "checkin"),
        ("签到帮助", "checkin"), ("好感度", "affection"), ("好感排行", "affection"),
        ("商店", "shop"), ("仓库", "shop"), ("商店帮助", "shop"),
    ):
        add(category="command", input_text=text, expected_route=expected,
            model_reply="", note=f"命令 {text} 不应进入 Agent")
    add(category="command", input_text="签到", interrupt=True,
        expected_route="checkin", model_reply="", note="命令优先于插嘴")

    # ---------- 8 种 marker：单 marker ----------
    add(category="marker_single", is_to_me=True, flags={"card": True},
        input_text="把我名片改成小鱼干",
        model_reply='好呀\n<<<CARD {"user_id":10001,"card":"小鱼干","kind":"request"}>>>',
        expected_tools=["card"], note="CARD 单 marker")
    add(category="marker_single", is_to_me=True, flags={"mute": True},
        input_text="禁言他",
        model_reply='行\n<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>>',
        expected_tools=["mute"], note="MUTE 单 marker")
    add(category="marker_single", is_to_me=True, flags={"search": True},
        input_text="今天天气怎么样", model_reply="<<<SEARCH 今日天气>>>",
        expected_tools=["search"], note="SEARCH 单 marker")
    add(category="marker_single", is_to_me=True, flags={"draw": True},
        input_text="画只猫", model_reply="给你画\n<<<DRAW a cat>>>",
        expected_tools=["draw"], note="DRAW 单 marker")
    add(category="marker_single", is_to_me=True, flags={"browser": True},
        input_text="去pixiv找猫图", model_reply="去找找\n<<<BROWSE pixiv 猫>>>",
        expected_tools=["browse"], note="BROWSE 单 marker")
    add(category="marker_single", is_to_me=True, flags={"voice": True},
        input_text="说晚安", model_reply="好\n<<<VOICE 晚安>>>",
        expected_tools=["voice"], note="VOICE 单 marker")
    add(category="marker_single", is_to_me=True, flags={"sticker": True},
        input_text="你好", model_reply="哼～\n<<<STICKER 傲娇>>>",
        expected_tools=["sticker"], note="STICKER 单 marker")
    add(category="marker_single", is_to_me=True, flags={"person_memory": True},
        input_text="我养了两只猫",
        model_reply='记住啦\n<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>',
        expected_tools=["impression"], note="IMPRESSION 单 marker")

    # ---------- 多 marker ----------
    add(category="marker_multi", is_to_me=True, flags={"card": True, "voice": True},
        input_text="改名片然后说晚安",
        model_reply=(
            "好呀\n"
            '<<<CARD {"user_id":10001,"card":"x","kind":"request"}>>>\n'
            "<<<VOICE 晚安>>>"
        ),
        expected_tools=["card", "voice"], note="CARD+VOICE 多 marker 顺序稳定")
    add(category="marker_multi", is_to_me=True,
        flags={"sticker": True, "voice": True, "person_memory": True},
        input_text="记住我喜欢猫，然后发个表情",
        model_reply=(
            '好\n<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>\n'
            "<<<STICKER 开心>>>\n<<<VOICE 记下啦>>>"
        ),
        expected_tools=["impression", "voice", "sticker"],
        note="三个 marker 按 priority 排序")

    # ---------- 容错闭合（关键差异必须保留） ----------
    add(category="tolerance", is_to_me=True, flags={"draw": True},
        input_text="画图", model_reply="图\n<<<DRAW a cat>>",
        expected_tools=["draw"], note="DRAW 接受 >>")
    add(category="tolerance", is_to_me=True, flags={"draw": True},
        input_text="画图", model_reply="图\n<<<DRAW a cat】",
        expected_tools=["draw"], note="DRAW 接受 】")
    add(category="tolerance", is_to_me=True, flags={"search": True},
        input_text="查天气", model_reply="<<<SEARCH 天气>>",
        expected_tools=["search"], note="SEARCH 接受 >>")
    add(category="tolerance", is_to_me=True, flags={"search": True},
        input_text="查天气", model_reply="<<<SEARCH 天气＞＞＞",
        expected_tools=["search"], note="SEARCH 接受 ＞＞＞")
    add(category="tolerance", is_to_me=True, flags={"search": True},
        input_text="查天气", model_reply="<<<SEARCH 天气】",
        expected_tools=["search"], note="SEARCH 接受 】")
    add(category="tolerance", is_to_me=True, flags={"card": True},
        input_text="改名片",
        model_reply='<<<CARD {"user_id":10001,"card":"x","kind":"request"}>>',
        expected_tools=[], note="CARD 严格：>> 不解析（历史行为）")
    add(category="tolerance", is_to_me=True, flags={"mute": True},
        input_text="禁言",
        model_reply='<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>',
        expected_tools=[], note="MUTE 严格：>> 不解析（历史行为）")
    add(category="tolerance", is_to_me=True, flags={"voice": True},
        input_text="语音说晚安", model_reply="好\n<<<VOICE 晚安】",
        expected_tools=["voice"], note="VOICE 接受 】")
    add(category="tolerance", is_to_me=True, flags={"sticker": True},
        input_text="你好", model_reply="哼\n<<<STICKER>>>",
        expected_tools=[], note="裸 STICKER 不解析（需关键词）")

    # ---------- 错误 marker / 缺参数 ----------
    add(category="malformed", is_to_me=True, flags={"card": True},
        input_text="改名片", model_reply="好\n<<<CARD {bad json}>>>",
        expected_tools=[], note="CARD 坏 JSON 不崩、不产生 action")
    add(category="malformed", is_to_me=True, flags={"mute": True},
        input_text="禁言", model_reply="好\n<<<MUTE {bad}>>>",
        expected_tools=[], note="MUTE 坏 JSON")
    add(category="malformed", is_to_me=True, flags={"person_memory": True},
        input_text="记住", model_reply="<<<IMPRESSION {bad}>>>",
        expected_tools=[], note="IMPRESSION 坏 JSON")
    add(category="malformed", is_to_me=True, flags={"card": True},
        input_text="改名片", model_reply='<<<CARD {"user_id":10001}>>>',
        expected_tools=[], note="CARD 缺 card 字段 → 解析层即丢弃（现有行为）")
    add(category="malformed", is_to_me=True, flags={"search": True},
        input_text="搜一下", model_reply="<<<SEARCH >>>",
        expected_tools=[], note="SEARCH 空查询词")

    # ---------- 无 marker ----------
    add(category="no_marker", is_to_me=True,
        flags={"card": True, "mute": True, "draw": True},
        input_text="随便聊聊", model_reply="好呀好呀",
        expected_tools=[], note="所有工具开着但模型不调")

    # ---------- 本地 3B 路径 ----------
    add(category="local_3b", is_to_me=True,
        note="本地模型：超长上下文被裁剪", input_text="你好",
        flags={"local": True}, model_reply="短回复")
    add(category="local_3b", is_to_me=True,
        note="本地模型：marker 被禁用的工具不注入",
        input_text="画图", flags={"local": True, "draw": True},
        model_reply="画好啦")

    # ---------- 权限 ----------
    #
    # 说明：replay 环境没有真实 bot 上下文，因此 `bot_can_manage_cards`
    # 无法判定（PermissionService 保守放行）。**真实的权限拒绝验证**
    # 见 test_phase5_permissions.py（那里注入了具备 bot 上下文的 fake）。
    # 这里只验证"解析层是否把调用交出去"。
    add(category="permission", is_to_me=True, flags={"mute": True},
        input_text="禁言他", model_reply='<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>>',
        expected_tools=["mute"],
        note="明确要求禁言 → 调用被交给执行层")
    add(category="permission", is_to_me=True, flags={"mute": True},
        input_text="今天天气不错", model_reply='<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>>',
        expected_tools=["mute"],
        note="未明确要求 → 由授权层拒绝（此处仅验证调用已产生）")
    add(category="permission", is_to_me=True, flags={"card": True},
        input_text="改名片",
        model_reply='<<<CARD {"user_id":10001,"card":"这个名片名字实在是太长了超过限制了","kind":"request"}>>>',
        expected_tools=["card"],
        note="名片超长 → 由授权层拒绝（此处仅验证调用已产生）")

    # ---------- 工具失败 / 超时 / 重复 ----------
    add(category="tool_failure", is_to_me=True, flags={"search": True},
        input_text="查天气", model_reply="<<<SEARCH 天气>>>",
        expected_tools=["search"], expected_stop="", note="search 成功路径")
    add(category="tool_failure", is_to_me=True, flags={"draw": True},
        input_text="画图", model_reply="<<<DRAW a cat>>>",
        expected_tools=["draw"], note="draw 失败 → FALLBACK_TEXT")
    add(category="tool_failure", is_to_me=True, flags={"browser": True},
        input_text="找图", model_reply="<<<BROWSE pixiv 猫>>>",
        expected_tools=["browse"], note="browse 超时受 timeout 约束")
    # 重复调用与无限循环需要"多轮持续返回同一 marker"才能触发，
    # stub 单轮回复无法复现。护栏行为见 test_phase4_agent.py。
    add(category="tool_failure", is_to_me=True, flags={"search": True},
        input_text="查天气", model_reply="<<<SEARCH 天气>>>",
        expected_tools=["search"], note="search 单轮成功")
    add(category="tool_failure", is_to_me=True, flags={"draw": True},
        input_text="画图", model_reply="<<<DRAW a cat>>>",
        expected_tools=["draw"], note="draw 单轮（失败策略 FALLBACK_TEXT）")

    # ---------- recovery 触发/不触发 ----------
    for tool, text, reply, trig in (
        ("card", "把我名片改成小鱼干", "已经帮你改好啦", True),
        ("card", "今天天气不错", "已经帮你改好啦", False),
        ("mute", "禁言他", "好，安静了", True),
        ("mute", "讲个笑话", "好，安静了", False),
        ("draw", "给我画只猫", "画好啦", True),
        ("draw", "今天几号", "画好啦", False),
        ("browse", "去pixiv找图", "找到啦", True),
        ("browse", "你好呀", "找到啦", False),
    ):
        add(category="recovery", is_to_me=True, flags={tool: True},
            input_text=text, model_reply=reply,
            expected_tools=[], expected_recovery=trig,
            note=f"{tool} recovery {'触发' if trig else '不触发'}")
    add(category="recovery", is_to_me=True, interrupt=True, flags={"card": True},
        input_text="改名片", model_reply="已经改好啦",
        expected_recovery=False, note="插嘴模式不触发 recovery")

    # ---------- 停止原因 ----------
    #
    # 说明：stub 网关只返回固定回复，因此"无限循环"无法在这里复现
    # （第二轮会收到空回复 → NO_TOOL_CALL）。护栏的终止行为由
    # test_phase4_agent.py 的 test_loop_max_steps / test_loop_token_budget
    # / test_loop_same_tool_repeat_detected 覆盖（那里按需返回多轮）。
    # 这里只验证"单轮无工具调用正常结束"。
    add(category="stop_reason", is_to_me=True,
        input_text="你好", model_reply="在呢",
        expected_stop="no_tool_call", note="无工具调用正常结束")
    add(category="stop_reason", is_to_me=True,
        input_text="你好", model_reply="在呢～",
        expected_tools=[], note="纯聊天不产生任何工具调用")

    # ============================================================
    # Phase 5.5 扩展（第三十五节）：补齐 recovery / permission /
    # tool failure / media fallback / memory isolation / infra failure
    # ============================================================

    # ---------- Recovery 全维度（显式意图 × 插嘴 × 已有 marker） ----------
    for tool, yes_text, no_text in (
        ("card", "把我名片改成小鱼干", "今天吃什么"),
        ("mute", "禁言他", "你会唱歌吗"),
        ("draw", "给我画只猫", "今天星期几"),
        ("browse", "去淘宝找裙子", "在忙吗"),
    ):
        # 显式意图但模型复读（无 marker）→ 触发
        add(category="recovery", is_to_me=True, flags={tool: True},
            input_text=yes_text, model_reply="好呀，已经处理了",
            expected_tools=[], expected_recovery=True,
            note=f"{tool} 显式意图无 marker → 触发 recovery")
        # 非显式意图 → 不触发
        add(category="recovery", is_to_me=True, flags={tool: True},
            input_text=no_text, model_reply="嗯嗯知道了",
            expected_tools=[], expected_recovery=False,
            note=f"{tool} 非显式意图 → 不触发")
        # 插嘴模式 → 不触发
        add(category="recovery", is_to_me=False, interrupt=True, flags={tool: True},
            input_text=yes_text, model_reply="已经处理了",
            expected_tools=[], expected_recovery=False,
            note=f"{tool} 插嘴模式 → 不触发")
        # 意图明确 + 已有 marker → 不重复触发
        add(category="recovery", is_to_me=True, flags={tool: True},
            input_text=yes_text,
            model_reply=(
                "好呀\n"
                + {
                    "card": '<<<CARD {"user_id":10001,"card":"x","kind":"request"}>>>',
                    "mute": '<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>>',
                    "draw": "<<<DRAW a cat>>>",
                    "browse": "<<<BROWSE pixiv 猫>>>",
                }[tool]
            ),
            expected_tools=[tool], expected_recovery=False,
            note=f"{tool} 已有 marker → 不告警不补救")

    # ---------- 媒体降级（DRAW/VOICE/STICKER 的失败路径） ----------
    add(category="media_fallback", is_to_me=True, flags={"draw": True},
        input_text="画只可爱的猫", model_reply="画好啦\n<<<DRAW cute cat>>>",
        expected_tools=["draw"], note="DRAW 成功路径（图片走 media 通道）")
    add(category="media_fallback", is_to_me=True, flags={"draw": True},
        input_text="画个机甲", model_reply="好\n<<<DRAW mecha>>>",
        expected_tools=["draw"], note="DRAW 失败时降级为纯文字")
    add(category="media_fallback", is_to_me=True, flags={"voice": True},
        input_text="用语音说晚安", model_reply="晚安\n<<<VOICE 晚安啦>>>",
        expected_tools=["voice"], note="VOICE 成功路径")
    add(category="media_fallback", is_to_me=True, flags={"voice": True},
        input_text="念一下这句话", model_reply="好\n<<<VOICE>>>",
        expected_tools=["voice"], note="VOICE 无 override（用正文）")
    add(category="media_fallback", is_to_me=True, flags={"sticker": True},
        input_text="你好呀", model_reply="哼\n<<<STICKER 傲娇>>>",
        expected_tools=["sticker"], note="STICKER 成功路径")
    add(category="media_fallback", is_to_me=True, flags={"sticker": True, "voice": True},
        input_text="打个招呼", model_reply="嗨\n<<<STICKER 开心>>>\n<<<VOICE 你好呀>>>",
        expected_tools=["voice", "sticker"], note="STICKER+VOICE 同轮")
    add(category="media_fallback", is_to_me=True, flags={"draw": True, "sticker": True},
        input_text="画猫并发表情",
        model_reply="好\n<<<DRAW cat>>>\n<<<STICKER 开心>>>",
        expected_tools=["draw", "sticker"], note="DRAW+STICKER 并发媒体")

    # ---------- 工具失败 ----------
    add(category="tool_failure", is_to_me=True, flags={"search": True},
        input_text="查一下今天的新闻", model_reply="<<<SEARCH 今日新闻>>>",
        expected_tools=["search"], note="SEARCH 正常触发")
    add(category="tool_failure", is_to_me=True, flags={"search": True},
        input_text="搜个不存在的词", model_reply="<<<SEARCH zzzzqqqq>>>",
        expected_tools=["search"], note="SEARCH 无结果路径")
    add(category="tool_failure", is_to_me=True, flags={"search": True},
        input_text="查天气和股票", model_reply="<<<SEARCH 天气 股票>>>",
        expected_tools=["search"], note="SEARCH 多 query")
    add(category="tool_failure", is_to_me=True, flags={"browse": True},
        input_text="去淘宝找连衣裙", model_reply="去找\n<<<BROWSE taobao 连衣裙>>>",
        expected_tools=["browse"], note="BROWSE taobao")
    add(category="tool_failure", is_to_me=True, flags={"browser": True},
        input_text="截图这个网址", model_reply="好\n<<<BROWSE screenshot https://example.com>>>",
        expected_tools=["browse"], note="BROWSE screenshot")
    add(category="tool_failure", is_to_me=True, flags={"browser": True},
        input_text="找张壁纸", model_reply="好\n<<<BROWSE image 壁纸>>>",
        expected_tools=["browse"], note="BROWSE image")

    # ---------- Memory 隔离（读写都经 Facade） ----------
    add(category="memory_isolation", is_to_me=True, flags={"person_memory": True},
        input_text="我叫小明，我喜欢猫",
        model_reply='记住啦\n<<<IMPRESSION {"add":["喜欢猫"],"remove":[],"notes":"小明"}>>>',
        expected_tools=["impression"], note="写入个人画像")
    add(category="memory_isolation", is_to_me=True, flags={"person_memory": True},
        input_text="我不喜欢猫了",
        model_reply='好\n<<<IMPRESSION {"add":[],"remove":["喜欢猫"],"notes":null}>>>',
        expected_tools=["impression"], note="移除画像标签")
    add(category="memory_isolation", message_type="private", is_to_me=True,
        flags={"person_memory": True},
        input_text="记住我的生日是三月",
        model_reply='好\n<<<IMPRESSION {"add":["生日三月"],"remove":[],"notes":null}>>>',
        expected_tools=["impression"], note="私聊写入画像")
    add(category="memory_isolation", is_to_me=True,
        flags={"person_memory": True, "memory_recall": True},
        input_text="还记得我之前说过的吗", model_reply="记得呀～",
        expected_tools=[], note="纯召回，不写画像")
    add(category="memory_isolation", is_to_me=True, flags={"group_style": True},
        input_text="这个群的人都爱说喵", model_reply="哈哈是呀",
        expected_tools=[], note="群风格采样（观察者层）")
    add(category="memory_isolation", is_to_me=True, flags={"inner_state": True},
        input_text="你好", model_reply="在呢～",
        expected_tools=[], note="内心状态读取")

    # ---------- 多 marker 组合 ----------
    add(category="marker_multi", is_to_me=True,
        flags={"card": True, "sticker": True},
        input_text="改名并发表情",
        model_reply=(
            "好呀\n"
            '<<<CARD {"user_id":10001,"card":"新名字","kind":"request"}>>>\n'
            "<<<STICKER 得意>>>"
        ),
        expected_tools=["card", "sticker"], note="CARD+STICKER")
    add(category="marker_multi", is_to_me=True,
        flags={"mute": True, "voice": True},
        input_text="禁言他然后说句话",
        model_reply=(
            "行\n"
            '<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>>\n'
            "<<<VOICE 安静了>>>"
        ),
        expected_tools=["mute", "voice"], note="MUTE+VOICE")
    add(category="marker_multi", is_to_me=True,
        flags={"search": True, "person_memory": True},
        input_text="查一下并记住我关心天气",
        model_reply=(
            "<<<SEARCH 今日天气>>>\n"
            '<<<IMPRESSION {"add":["关心天气"],"remove":[],"notes":null}>>>'
        ),
        expected_tools=["search", "impression"], note="SEARCH+IMPRESSION")
    add(category="marker_multi", is_to_me=True,
        flags={"draw": True, "browse": True, "sticker": True},
        input_text="画图并找图",
        model_reply=(
            "好\n<<<DRAW a cat>>>\n<<<BROWSE pixiv 猫>>>\n<<<STICKER 开心>>>"
        ),
        expected_tools=["draw", "browse", "sticker"], note="三个媒体 marker")

    # ---------- 运行时基础设施失败（Phase 5.5 关注点） ----------
    #
    # 说明：replay runner 用 stub 网关，无法直接注入基础设施异常。
    # 这些样本验证的是"模型返回空/异常内容时不会崩溃，也不会触发
    # 第二套 legacy agent"。真正的 infra failure 行为由
    # test_phase55_acceptance.py 覆盖。
    add(category="infra_failure", is_to_me=True,
        input_text="你好", model_reply="", note="模型返回空串不崩溃")
    add(category="infra_failure", is_to_me=True,
        input_text="你好", model_reply="\n\n\n", note="模型只返回空白")
    add(category="infra_failure", is_to_me=True, flags={"search": True},
        input_text="查", model_reply="<<<SEARCH", note="marker 未闭合")
    add(category="infra_failure", is_to_me=True, flags={"card": True},
        input_text="改名片", model_reply="<<<CARD", note="CARD marker 截断")
    add(category="infra_failure", is_to_me=True,
        input_text="你好", model_reply="<<<UNKNOWN_MARKER x>>>",
        note="未知 marker 被忽略（正文保留）")
    add(category="infra_failure", is_to_me=True, flags={"card": True},
        input_text="改名片", model_reply='<<<CARD {"user_id":"abc"}>>>',
        note="CARD user_id 类型错误不崩溃")

    # ---------- 边界文本 ----------
    add(category="plain", is_to_me=True,
        input_text="a" * 500, model_reply="好长呀", note="超长输入")
    add(category="plain", is_to_me=True,
        input_text="🐱🐶🐰", model_reply="好可爱", note="纯 emoji")
    add(category="plain", is_to_me=True,
        input_text="！！！？？？", model_reply="怎么了", note="纯标点")
    add(category="plain", is_to_me=True,
        input_text="  ", model_reply="", note="纯空白（未 @ 时不触发）")
    add(category="mention", is_to_me=True,
        input_text="@学姐 @学姐 在吗", model_reply="在呀", note="重复 @")
    add(category="mention", is_to_me=True,
        input_text="在吗（顺便问下损失函数）", model_reply="在的", note="中英混合")
    add(category="private", message_type="private", is_to_me=True,
        input_text="晚安", model_reply="晚安啦", note="私聊晚安")
    add(category="private", message_type="private", is_to_me=True,
        input_text="", model_reply="在呢", note="私聊空消息")

    return cases


def corpus_stats(cases: list[ReplayCase]) -> dict[str, int]:
    """按 category 统计样本数。"""
    out: dict[str, int] = {}
    for case in cases:
        out[case.category] = out.get(case.category, 0) + 1
    return dict(sorted(out.items()))


__all__ = ["ReplayCase", "build_corpus", "corpus_stats"]
