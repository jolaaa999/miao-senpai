from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from typing import ClassVar

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .config_runtime.providers import (
    ConfigProvider,
    ProductionConfigProvider,
    TestConfigProvider,
    TEST_MODE_ENV,
    default_provider,
    is_test_mode,
)

# Edge TTS 预设：安洁莉娜 = 元气信使少女（活泼略快、音调稍高）
_VOICE_PROFILES: dict[str, dict[str, str]] = {
    "angelina": {
        "name": "zh-CN-XiaoyiNeural",
        "rate": "+14%",
        "pitch": "+8Hz",
    },
}


@dataclass(frozen=True)
class ResolvedVoiceSettings:
    name: str
    rate: str
    pitch: str
    profile: str


class PluginConfig(BaseSettings):
    """学姐插件配置（环境变量前缀 DL_SENPAI_ / OPENAI_ / OLLAMA_ / LLM_）。

    配置来源优先级（**高 → 低**）::

        1. 显式 kwargs    PluginConfig(OPENAI_API_KEY="sk-test")
        2. Provider 覆盖  TestConfigProvider(overrides={...})
        3. 进程环境变量   os.environ（可被 Provider 关闭）
        4. .env / .env.dev 文件（可被 Provider 清空）

    这个顺序是**修复测试污染的关键**：重构前 `.env` 的优先级高于显式
    kwargs，导致 `PluginConfig(OPENAI_API_KEY="sk-test")` 拿到的是真实的
    中转 Key、真实 base_url、真实 speak style 里的本地模型端点，
    单测因此真的发出了网络请求（并产生真实费用）。

    现在由 `_active_provider` 决定读哪些来源：
    - 生产：与重构前完全一致（.env + .env.dev + 环境变量）；
    - 测试（`DL_SENPAI_TEST_MODE=1` 或显式 `use_config_provider()`）：
      不读任何文件、不读任何环境变量。
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.dev"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # 当前生效的配置来源。
    #
    # 注意：**不能**声明成类属性 `_active_provider: ConfigProvider | None = None`，
    # 因为 pydantic 会把带类型注解的类属性收编为 `ModelPrivateAttr`，
    # 于是 `cls._active_provider` 拿到的是描述符而非 provider 本身
    # （表现为 `'ModelPrivateAttr' object has no attribute 'read_os_environ'`）。
    # 这里改用 `ClassVar` 显式排除出模型字段，pydantic 就不会处理它。
    _active_provider: ClassVar[ConfigProvider | None] = None

    @classmethod
    def use_config_provider(cls, provider: ConfigProvider | None) -> None:
        """切换配置来源。传 None 恢复「按环境自动选择」。"""
        cls._active_provider = provider

    @classmethod
    def current_provider(cls) -> ConfigProvider:
        """取当前来源；未显式设置时按环境自动选择。"""
        provider = cls.__dict__.get("_active_provider")
        if provider is not None:
            return provider
        return default_provider()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """按 Provider 重新排列配置来源。

        返回顺序 = 优先级从高到低。pydantic-settings 会依次取值，先命中的生效。

        生产模式下返回 `(init, env, dotenv)`——与重构前的默认顺序一致，
        保证现有部署行为零变化。

        测试模式下只返回 `(init,)`，彻底切断 .env 与环境变量这两条泄漏路径。
        """
        provider = cls.current_provider()
        sources: list[PydanticBaseSettingsSource] = [init_settings]
        if provider.read_os_environ:
            sources.append(env_settings)
        if provider.dotenv_files():
            sources.append(dotenv_settings)
        return tuple(sources)

    # relay = 中转站（OPENAI_*）；ollama = 本地 Ollama（仅 LLM_PROVIDER=ollama 时用，不再自动回退）
    llm_provider: str = Field(default="relay", alias="LLM_PROVIDER")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-6-astra", alias="OPENAI_MODEL")

    # 仅当 LLM_PROVIDER=ollama 时生效；Key 为空则读系统环境变量 API_Key
    ollama_api_key: str = Field(default="", alias="OLLAMA_API_KEY")
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434/v1", alias="OLLAMA_BASE_URL"
    )
    ollama_model: str = Field(default="qwen2.5:7b", alias="OLLAMA_MODEL")

    # 插嘴总开关。关掉后群里不再主动搭话，只有 @ / 私聊 / 召唤词才回复。
    # 为什么不直接把 interrupt_prob 改成 0：decide_trigger 还会在概率上叠加
    # keyword_boost（命中关键词 +0.05）和 image_interrupt_boost（带图 +0.06），
    # 概率永远压不到 0，照样会插嘴。做成独立开关，重开时其余参数原样生效。
    interrupt_enable: bool = Field(default=True, alias="DL_SENPAI_INTERRUPT_ENABLE")
    interrupt_prob: float = Field(default=0.10, alias="DL_SENPAI_INTERRUPT_PROB")
    interrupt_cooldown: int = Field(default=90, alias="DL_SENPAI_INTERRUPT_COOLDOWN")
    # 刚 @ 学姐后，多少秒内再发表情包也算找她（QQ 不便同条 @+表情）
    sticker_followup_sec: int = Field(default=60, alias="DL_SENPAI_STICKER_FOLLOWUP_SEC")
    min_msg_len: int = Field(default=8, alias="DL_SENPAI_MIN_MSG_LEN")
    # 召唤词：群聊/私聊/临时会话里只要**包含**这些词（逗号分隔），无需 @ 也强制回复。
    # 放在 min_msg_len 旁边而不是别处，因为它属于触发判定：命中召唤词会绕过长度
    # 过滤 ——「喵喵」只有 2 字，短于 min_msg_len，否则会被当噪音丢掉。
    summon_keywords: str = Field(
        default="喵喵,喵学姐", alias="DL_SENPAI_SUMMON_KEYWORDS"
    )
    max_history: int = Field(default=0, alias="DL_SENPAI_MAX_HISTORY")
    # 喂给模型的历史字符预算（0 = 不限）。只影响「读给模型」，
    # 不会删磁盘上的记忆——用户聊天记录必须留着。
    history_max_chars: int = Field(default=12000, alias="DL_SENPAI_HISTORY_MAX_CHARS")
    # 落盘侧滚动上限（条数，0 = 不限）。为什么不靠上面的 max_history：
    # max_history 只裁剪「读给模型」那一份，磁盘文件仍会无限长——
    # 实测有个私聊会话攒到 343 条。文件越长，每轮 append 都要整份重写，
    # 而且旧人设时期的原文会一直躺在里面被反复读到。
    # 被滚出去的消息不会丢：追加到 memory/.rollover/<session>.jsonl。
    memory_persist_max_messages: int = Field(
        default=120, alias="DL_SENPAI_MEMORY_PERSIST_MAX_MESSAGES"
    )
    # 1.1 对齐参考实现（brain.js）。低于 0.9 时措辞会稳定到「像模板」的程度：
    # 安全但没味道，日常闲聊一眼看出是机器。
    temperature: float = Field(default=1.1, alias="DL_SENPAI_TEMPERATURE")
    max_tokens: int = Field(default=1600, alias="DL_SENPAI_MAX_TOKENS")
    interrupt_max_tokens: int = Field(default=200, alias="DL_SENPAI_INTERRUPT_MAX_TOKENS")
    reply_max_chars: int = Field(default=1500, alias="DL_SENPAI_REPLY_MAX_CHARS")
    reply_chunk_chars: int = Field(default=900, alias="DL_SENPAI_REPLY_CHUNK_CHARS")

    # 正文超过这个字数就用合并转发（聊天记录卡片）发，避免连发气泡刷屏；
    # 0 = 关闭，退回原来的「按 reply_chunk_chars 切段再转发」行为
    forward_min_chars: int = Field(default=400, alias="DL_SENPAI_FORWARD_MIN_CHARS")
    # 合并转发里每一节的字数上限：手机上超过这个长度要滑好几屏
    forward_node_chars: int = Field(default=600, alias="DL_SENPAI_FORWARD_NODE_CHARS")
    # 硬上限，0 = 不截断（默认）。要有这个上限，是为了防模型跑飞把整段话灌进群里，
    # 不是为了控长度——控长度靠提示词里的长度纪律。
    # 注意：真触发时只在句子边界切，绝不切半句话（见 llm.truncate_reply）。
    reply_hard_max_chars: int = Field(
        default=0, alias="DL_SENPAI_REPLY_HARD_MAX_CHARS"
    )
    allowed_groups: str = Field(default="", alias="DL_SENPAI_ALLOWED_GROUPS")
    enable_private: bool = Field(default=True, alias="DL_SENPAI_ENABLE_PRIVATE")
    welcome_enable: bool = Field(default=True, alias="DL_SENPAI_WELCOME_ENABLE")

    # 新人认证：进群后限时 @学姐 说「认证」，超时踢出（需学姐是群管）
    verify_enable: bool = Field(default=True, alias="DL_SENPAI_VERIFY_ENABLE")
    verify_timeout_sec: int = Field(default=600, alias="DL_SENPAI_VERIFY_TIMEOUT_SEC")
    verify_groups: str = Field(
        default="",
        alias="DL_SENPAI_VERIFY_GROUPS",
        description="启用认证的群，逗号分隔；空=所有白名单/全部群",
    )
    verify_dir: str = Field(
        default="data/dl_senpai/verify", alias="DL_SENPAI_VERIFY_DIR"
    )
    keyword_boost: float = Field(default=0.05, alias="DL_SENPAI_KEYWORD_BOOST")
    memory_dir: str = Field(default="data/dl_senpai/memory", alias="DL_SENPAI_MEMORY_DIR")

    # 长期记忆·概览层（双层记忆的上层，见 agent/memory/cards.py）。
    # 与 memory_recall 的分工：这里是结构化事实卡片，常驻上下文给全局视角；
    # memory_recall 是按需从原始对话里取回的细节。
    memory_cards_enable: bool = Field(
        default=True, alias="DL_SENPAI_MEMORY_CARDS_ENABLE"
    )
    memory_cards_dir: str = Field(
        default="data/dl_senpai/memory_cards", alias="DL_SENPAI_MEMORY_CARDS_DIR"
    )
    memory_cards_max: int = Field(default=12, alias="DL_SENPAI_MEMORY_CARDS_MAX")
    memory_cards_max_chars: int = Field(
        default=700, alias="DL_SENPAI_MEMORY_CARDS_MAX_CHARS"
    )
    # 后台提取：每 N 轮跑一次 LLM 抽事实卡。取模触发，不是每次都抽——
    # 每次提取都是一次额外调用，批量做才摊得平成本（Memobase 的做法）。
    memory_extract_enable: bool = Field(
        default=True, alias="DL_SENPAI_MEMORY_EXTRACT_ENABLE"
    )
    memory_extract_every: int = Field(
        default=6, alias="DL_SENPAI_MEMORY_EXTRACT_EVERY"
    )

    # ---- 高风险工具：文件 / 命令 / 代码（agent/tools/file_tools.py、exec_tools.py）----
    #
    # 这一组工具的能力远超「聊天机器人」——它们能读写磁盘、执行任意命令。
    # 因此**只有管理员 QQ 能用**（agent_admin_qq），群友既看不到也调不到。
    # 判定发生在 permissions.HIGH_RISK_TOOLS 那一层，不是靠这里的开关。
    #
    # agent_admin_qq 为空时 → 高风险工具对**所有人**都不可见。
    # 这个默认是有意为之：管理员没配置就当作没开通，而不是当作人人可用。
    agent_admin_qq: str = Field(default="", alias="DL_SENPAI_AGENT_ADMIN_QQ")
    # 工具总开关。关掉它，7 个高风险工具一起消失，不用逐个改。
    agent_tools_enable: bool = Field(
        default=True, alias="DL_SENPAI_AGENT_TOOLS_ENABLE"
    )
    agent_file_enable: bool = Field(
        default=True, alias="DL_SENPAI_AGENT_FILE_ENABLE"
    )
    agent_shell_enable: bool = Field(
        default=True, alias="DL_SENPAI_AGENT_SHELL_ENABLE"
    )
    agent_code_enable: bool = Field(
        default=True, alias="DL_SENPAI_AGENT_CODE_ENABLE"
    )
    # 沙箱根目录。所有文件路径都必须落在它之内，命令也以它为工作目录。
    agent_sandbox_dir: str = Field(
        default="data/dl_senpai/workspace", alias="DL_SENPAI_AGENT_SANDBOX_DIR"
    )
    agent_shell_timeout: int = Field(
        default=30, alias="DL_SENPAI_AGENT_SHELL_TIMEOUT"
    )
    agent_code_timeout: int = Field(
        default=30, alias="DL_SENPAI_AGENT_CODE_TIMEOUT"
    )
    # 长输出裁剪的目标长度（保留头尾，见 sandbox.truncate_output）。
    agent_output_max_chars: int = Field(
        default=6000, alias="DL_SENPAI_AGENT_OUTPUT_MAX_CHARS"
    )
    agent_file_read_max_bytes: int = Field(
        default=200000, alias="DL_SENPAI_AGENT_FILE_READ_MAX_BYTES"
    )
    agent_glob_max: int = Field(default=60, alias="DL_SENPAI_AGENT_GLOB_MAX")

    # ---- Agent 循环预算（护栏阈值）----
    # 为什么做成配置项：通用 agent 的循环预算通常在几十步，而群聊场景
    # 90 秒内必须出声，所以默认压到 4 步；但管理员私聊做多文件整理这类
    # 长任务时需要能单独拉高上限，而不是改代码重启。
    agent_max_steps: int = Field(default=4, alias="DL_SENPAI_AGENT_MAX_STEPS")
    agent_max_tool_calls: int = Field(
        default=8, alias="DL_SENPAI_AGENT_MAX_TOOL_CALLS"
    )
    # 同一「工具+参数」重复几次就停：防模型陷入复读循环。
    agent_same_tool_repeat_limit: int = Field(
        default=2, alias="DL_SENPAI_AGENT_SAME_TOOL_REPEAT_LIMIT"
    )
    # 整轮墙钟超时（秒）。0 = 不限制。
    agent_timeout: float = Field(default=90.0, alias="DL_SENPAI_AGENT_TIMEOUT")
    # token 预算。0 = 不限制（默认，避免误杀正常长回复）。
    agent_token_budget: int = Field(
        default=0, alias="DL_SENPAI_AGENT_TOKEN_BUDGET"
    )
    # 原生 function calling。默认 false = 继续走 <<<MARKER>>> 文本协议。
    # 打开后 runtime 会把工具 schema 作为 tools 参数发给模型，并解析
    # tool_calls；请求体里不再注入 marker 说明。回退只需改回 false。
    agent_native_tools: bool = Field(
        default=False, alias="DL_SENPAI_AGENT_NATIVE_TOOLS"
    )
    # 每条请求一行 trace 摘要（DL_SENPAI_AGENT_TRACE_SUMMARY）。
    #
    # 为什么默认开：现有的 telemetry 已经把工具/模型/发送/记忆全采集了，
    # 但**没有任何一行日志落地** —— 只有打开 DL_SENPAI_VALIDATION 时才写
    # JSONL，且要跑离线脚本才看得见。线上排查「这条消息为什么这样回」
    # 时手里没有可 grep 的东西。这一行就是给那个场景的。
    # 关掉它不影响任何行为，只是少一行日志。
    agent_trace_summary: bool = Field(
        default=True,
        alias="DL_SENPAI_AGENT_TRACE_SUMMARY",
        description="每条请求结束输出一行可 grep 的 trace 摘要",
    )

    # 拟人化：口语 few-shot / 碎句发送 / 内心状态 / 群风格 / 记忆召回
    fewshot_enable: bool = Field(default=True, alias="DL_SENPAI_FEWSHOT_ENABLE")
    humanize_send: bool = Field(default=True, alias="DL_SENPAI_HUMANIZE_SEND")
    # 下限从 400 提到 900：真人回一句「嗯」也要看一眼、抬手、打完再发，
    # 400ms 那种秒回反而假。ai-companion 的 Humanizer 用的是 1200ms。
    typing_delay_ms_min: int = Field(default=900, alias="DL_SENPAI_TYPING_DELAY_MS_MIN")
    typing_delay_ms_max: int = Field(default=1200, alias="DL_SENPAI_TYPING_DELAY_MS_MAX")
    # 一次回复最多拆成几条气泡。**默认 3 = 拆**（2026-10-25 改）。
    #
    # 为什么从 1 改回来：真正的活人是一条一条蹦的，不是把一段话压在一条里发。
    # 以前关掉它的理由是「把一句话切成三条发像刷屏」——那个判断对，但问题在
    # 上游：模型写了一大段，出站再按标点切，切出来当然像被剁碎的段落。
    # 现在的做法是让模型先说短句（一条一行），出站只按**它自己分的行**拆泡，
    # 单段长文不硬切（见 bubble_require_newline）。这样拆出来的是三句独立的话，
    # 不是一段话的三块。
    bubble_max: int = Field(default=3, alias="DL_SENPAI_BUBBLE_MAX")
    # 下限从 40 降到 8：闲聊本来就短，40 的门槛等于「短回复永不拆」，
    # 而短回复恰恰最该分成两三口发。
    bubble_min_chars: int = Field(default=8, alias="DL_SENPAI_BUBBLE_MIN_CHARS")
    # 每个气泡的目标字数。32 ≈ 一句口语的长度；原来 120 是把三句并成一条。
    bubble_prefer_chars: int = Field(default=32, alias="DL_SENPAI_BUBBLE_PREFER_CHARS")
    # 合计字数超过它就不再拆泡、整条单发（不是转卡片）：
    # 160 字以上通常是正经答疑，拆成三条每条约 60 字才是真刷屏。
    bubble_total_max_chars: int = Field(default=160, alias="DL_SENPAI_BUBBLE_TOTAL_MAX_CHARS")
    # 只按模型自己分的行拆泡，单段长文绝不按标点硬切。
    # 这一条是「拆泡」和「剁碎」的分界线：有换行 = 她本来就想分两句发；
    # 没换行 = 她就是想一口气说完，切了就是替她做决定。
    bubble_require_newline: bool = Field(
        default=True, alias="DL_SENPAI_BUBBLE_REQUIRE_NEWLINE"
    )

    # -- 发送闸门：熔断 + 频率上限（搬自 ai-companion-kit 第十三章防风控）--
    # 腾讯判断机器人最看的不是你打几个字，而是「失败之后还一直重试」。
    # 下面这几个默认值都是踩坑调出来的，别为了「响应更快」放大。
    send_breaker_enable: bool = Field(default=True, alias="DL_SENPAI_SEND_BREAKER_ENABLE")
    # 连续失败几次就熔断
    send_breaker_fail_threshold: int = Field(
        default=3, alias="DL_SENPAI_SEND_BREAKER_FAIL_THRESHOLD"
    )
    # 熔断后停多久（秒）。期间连尝试都不发起。
    send_breaker_cooldown_sec: int = Field(
        default=180, alias="DL_SENPAI_SEND_BREAKER_COOLDOWN_SEC"
    )
    # 0 = 不限。默认 6 条/分钟、80 条/小时。
    # 拆泡后一次回复要发 2~3 条，6 条/分钟等于两轮就撞墙（2026-10-25 提到 12）。
    # 没敢提太高：腾讯看的不是你打几个字，是短时间内发多少条。
    send_rate_per_minute: int = Field(default=12, alias="DL_SENPAI_SEND_RATE_PER_MINUTE")
    send_rate_per_hour: int = Field(default=160, alias="DL_SENPAI_SEND_RATE_PER_HOUR")

    # -- 真·「正在输入」（OneBot 扩展 set_input_status）--
    # 上报太勤本身就是机器人特征（原项目一天 175 次，反而暴露了）。
    # 所以默认：短消息不发、至少隔 45 秒、35% 概率。
    # NapCat 不一定实现这个接口，探到不支持会自动永久关闭。
    typing_status_enable: bool = Field(default=True, alias="DL_SENPAI_TYPING_STATUS_ENABLE")
    typing_status_min_chars: int = Field(
        default=20, alias="DL_SENPAI_TYPING_STATUS_MIN_CHARS"
    )
    typing_status_min_interval_sec: int = Field(
        default=45, alias="DL_SENPAI_TYPING_STATUS_MIN_INTERVAL_SEC"
    )
    typing_status_probability: float = Field(
        default=0.35, alias="DL_SENPAI_TYPING_STATUS_PROBABILITY"
    )
    # 打字时长按字数算（真人手打约 110ms/字）。0 = 仍用固定延迟区间。
    typing_ms_per_char: int = Field(default=110, alias="DL_SENPAI_TYPING_MS_PER_CHAR")
    # 单条最长等多久（毫秒）。封顶是为了别让长回复把人等跑。
    typing_ms_max: int = Field(default=3000, alias="DL_SENPAI_TYPING_MS_MAX")
    # 打字间隔抖动比例：0 = 完全按字数算（太规整，机器味），
    # 0.35 = 每次在 ±35% 之间浮动，同一句话两次能差一半。
    typing_jitter_ratio: float = Field(
        default=0.35, alias="DL_SENPAI_TYPING_JITTER_RATIO"
    )
    # 群里随口一句时，「看见了但不接」的概率。真人不会每条必回。
    # 只作用于非强制触发（插嘴/概率醒），@ 和私聊仍然必回。
    silence_prob_group: float = Field(
        default=0.1, alias="DL_SENPAI_SILENCE_PROB_GROUP"
    )

    inner_state_enable: bool = Field(default=True, alias="DL_SENPAI_INNER_STATE_ENABLE")
    inner_state_dir: str = Field(
        default="data/dl_senpai/inner_state", alias="DL_SENPAI_INNER_STATE_DIR"
    )
    inner_state_per_group: bool = Field(
        default=True, alias="DL_SENPAI_INNER_STATE_PER_GROUP"
    )

    # -- 她自己的日记（搬自 ai-companion-kit 的 diary）--
    # 记忆是「关于你的事」，日记是「她自己的一天」。两者分开存：
    # 记忆有硬上限，写满会顶掉旧条目；日记天天写、一年几百条，
    # 混在一起最后就是「她记得昨天酸奶酸了」却「忘了你是谁」。
    diary_enable: bool = Field(default=True, alias="DL_SENPAI_DIARY_ENABLE")
    diary_dir: str = Field(default="data/dl_senpai/diary", alias="DL_SENPAI_DIARY_DIR")
    # 注入提示词的条数。3 条就够：日记是「她昨天干了什么」，
    # 不是第二本记忆账本，全塞进去会把她拽在旧事里接不上眼前的话。
    diary_context_n: int = Field(default=3, alias="DL_SENPAI_DIARY_CONTEXT_N")
    diary_max_chars: int = Field(default=600, alias="DL_SENPAI_DIARY_MAX_CHARS")

    # -- 她自己的生活：定时静默唤醒（搬自 ai-companion-kit 的 own-life）--
    # 默认关闭。它是唯一一种「用户没开口、她自己往外发消息」的能力，
    # 必须由人显式打开，并且配合下面的白名单群才允许发言。
    # -- 主人：说话直接接，不受冷却和概率限制（搬自 ai-companion-kit 的 ownerIds）--
    # 逗号分隔 QQ 号。留空 = 没有主人，一切按概率/冷却走。
    # 为什么需要：群里她刚回过话、正在冷却时，你再说一句会被静默晾着——
    # 那种「明明在却不理我」比不回更伤。这里让指定的人始终能叫得动她。
    owner_ids: str = Field(default="", alias="DL_SENPAI_OWNER_IDS")

    # -- 本轮语气判定（吐槽 / 真倾诉）--
    # 人设里的「日常吐槽不是情绪求助」压不住（连「今天好累」都点名了照样安慰），
    # 所以每轮先判一次，再把结论贴到提示词最末。纯字符串匹配，不花 API。
    tone_classify_enable: bool = Field(
        default=True, alias="DL_SENPAI_TONE_CLASSIFY_ENABLE"
    )
    # 对方只是随口一句时，本轮追加的具体字数（0 = 不加）。
    # 通用规则「不超过 60 字」实测压不住，只有「本轮具体」这种才动得动。
    casual_reply_hint_chars: int = Field(
        default=30, alias="DL_SENPAI_CASUAL_REPLY_HINT_CHARS"
    )
    # 安慰腔的出站过滤：命中词表就把那个小句剪掉（不靠模型听话）。
    comfort_strip_enable: bool = Field(
        default=True, alias="DL_SENPAI_COMFORT_STRIP_ENABLE"
    )
    # 随口一句时，回复按**整句**累加到这个字数为止（0 = 不截）。
    # 只切在句末标点后，绝不吐半句话；第一句再长也照发。
    casual_trim_chars: int = Field(default=60, alias="DL_SENPAI_CASUAL_TRIM_CHARS")
    # 私聊的字数预算单独给：私聊本来就该多说两句，
    # 用群聊那个 60 会把她压得像个客服机器人（这是我第一版的漏项）。
    private_trim_chars: int = Field(default=150, alias="DL_SENPAI_PRIVATE_TRIM_CHARS")
    # 「随口一句」的字数上限：超过这个长度就不当作随口一句，不截断。
    casual_prompt_max_chars: int = Field(
        default=25, alias="DL_SENPAI_CASUAL_PROMPT_MAX_CHARS"
    )

    # -- 梗密度（见 meme.py）--
    # auto = 按本轮语气/话题自动定档；也可强制 off / light / normal / heavy。
    # 自动规则：真倾诉不用、正经答疑不用、对方在用梗就接、随口闲聊克制。
    meme_level: str = Field(default="auto", alias="DL_SENPAI_MEME_LEVEL")

    # -- 反 AI 味出站清洗（见 humanize.py）--
    # 严重档（AI 自指/免责/客服收尾/自曝检索/元话语）整句删，删空退回原句。
    humanize_enable: bool = Field(default=True, alias="DL_SENPAI_HUMANIZE_ENABLE")
    # 轻微档：首先/其次/综上所述 这类书面连接词。答疑时有用，闲聊时是味道。
    humanize_soft_enable: bool = Field(
        default=True, alias="DL_SENPAI_HUMANIZE_SOFT_ENABLE"
    )

    # -- 复盘腔 / 点评自己 的出站收缩（见 humanize.collapse_self_review）--
    # 「说实话，我刚才的回答确实偏了，抱歉。翻回去看……」这类自我诊断报告
    # 是最不像人的一类：被指出错误后写分段的 bug 说明。整段收成一句或者不发。
    self_review_collapse_enable: bool = Field(
        default=True, alias="DL_SENPAI_SELF_REVIEW_COLLAPSE_ENABLE"
    )
    # 闲聊时的口语替换（因此→所以、是否→是不是）。答疑长回复不走这条，
    # 那里书面连接词是正常表述。
    casual_speak_swaps_enable: bool = Field(
        default=True, alias="DL_SENPAI_CASUAL_SPEAK_SWAPS_ENABLE"
    )
    # 「像不像人」指标写日志（llm.ai_flavor）。只观测，不改写。
    # 要拿数据判断改提示词有没有用，就靠这一行日志。
    ai_flavor_log_enable: bool = Field(
        default=True, alias="DL_SENPAI_AI_FLAVOR_LOG_ENABLE"
    )
    # -- 跨场合对话流（见 cross_chat.py）--
    # 记忆按会话存 → 同一个人换个群就不认识；这一层按时间存，补这个洞。
    # 隐私规则写死在代码里（私聊内容不进群），没有开关。
    cross_chat_enable: bool = Field(
        default=False, alias="DL_SENPAI_CROSS_CHAT_ENABLE"
    )
    cross_chat_recent_n: int = Field(default=8, alias="DL_SENPAI_CROSS_CHAT_RECENT_N")
    cross_chat_max_chars: int = Field(
        default=800, alias="DL_SENPAI_CROSS_CHAT_MAX_CHARS"
    )
    cross_chat_max_entries: int = Field(
        default=600, alias="DL_SENPAI_CROSS_CHAT_MAX_ENTRIES"
    )
    cross_chat_dir: str | None = Field(default=None, alias="DL_SENPAI_CROSS_CHAT_DIR")

    # -- 重复消息去重 --
    # NapCat 断线重连会重放没 ack 的事件；不去重，她会对着同一句话回两遍。
    dedupe_enable: bool = Field(default=True, alias="DL_SENPAI_DEDUPE_ENABLE")
    # 判重窗口秒数：窗口内同 id 视为重复
    dedupe_window_sec: int = Field(default=300, alias="DL_SENPAI_DEDUPE_WINDOW_SEC")
    # 最多记多少条，超了按 FIFO 丢最老的（重连风暴时不能无限涨）
    dedupe_max_items: int = Field(default=512, alias="DL_SENPAI_DEDUPE_MAX_ITEMS")

    # -- 消息合并窗口（搬自 ai-companion-kit 的 Waker.batch）--
    # 对方连着发三条短消息时，等一小会儿攒成一批再回一次，
    # 而不是追着每条都答一句。
    # 注意跟 bubble_max 不是一回事：那个管「她的话拆几条」，这个管「对方的话攒几条」。
    batch_enable: bool = Field(default=True, alias="DL_SENPAI_BATCH_ENABLE")
    # 群聊窗口比私聊长：群里打字慢、常分几条说
    batch_window_group_ms: int = Field(
        default=2500, alias="DL_SENPAI_BATCH_WINDOW_GROUP_MS"
    )
    batch_window_private_ms: int = Field(
        default=1500, alias="DL_SENPAI_BATCH_WINDOW_PRIVATE_MS"
    )
    # 攒到这么多条就不再等，立刻交出去（防刷屏时一直不吭声）
    batch_max_items: int = Field(default=6, alias="DL_SENPAI_BATCH_MAX_ITEMS")

    # -- 干跑模式（搬自 ai-companion-kit 的 --dry-run）--
    # 打开后整条链路照跑（记忆、提示词、模型、工具），**只在最后发送那一步换成打日志**。
    # 用途：刚改完人设/长度，想连问十句看语气，但又不能在群里真的发出去。
    # 日志里搜「[干跑]」就能看到她会说什么。改完记得关掉。
    dry_run: bool = Field(default=False, alias="DL_SENPAI_DRY_RUN")

    # -- 回复长度纪律（注入到系统提示词最末）--
    # 原来人设里写着「答疑目标 400~1500 字」，群里问一句她回三段小作文。
    # 关掉就退回旧行为（话会明显变长），一般不该关。
    reply_brevity_enable: bool = Field(
        default=True, alias="DL_SENPAI_REPLY_BREVITY_ENABLE"
    )

    own_life_enable: bool = Field(default=False, alias="DL_SENPAI_OWN_LIFE_ENABLE")
    # 唤醒间隔（秒）。默认 9000 = 2.5 小时，原项目调出来的节奏：
    # 短于 1 小时会显得她在网吧通宵，长于半天就感觉不到「她有自己的日子」。
    own_life_interval_sec: int = Field(
        default=9000, alias="DL_SENPAI_OWN_LIFE_INTERVAL_SEC"
    )
    # 每天最多叫醒几次。被叫醒不等于必须说话，更不能一天六条。
    own_life_max_per_day: int = Field(
        default=6, alias="DL_SENPAI_OWN_LIFE_MAX_PER_DAY"
    )
    # 静默时段（本地时间，允许跨零点）。她凌晨也要睡觉。
    own_life_quiet_start: int = Field(
        default=3, alias="DL_SENPAI_OWN_LIFE_QUIET_START"
    )
    own_life_quiet_end: int = Field(default=8, alias="DL_SENPAI_OWN_LIFE_QUIET_END")
    # 允许主动发言的群号，逗号分隔。**留空 = 只写日记不发消息**。
    # 这条是硬闸门：主动消息是这个项目里唯一外发且撤不回的动作。
    own_life_groups: str = Field(default="", alias="DL_SENPAI_OWN_LIFE_GROUPS")
    own_life_dir: str = Field(
        default="data/dl_senpai/own_life", alias="DL_SENPAI_OWN_LIFE_DIR"
    )

    group_style_enable: bool = Field(default=True, alias="DL_SENPAI_GROUP_STYLE_ENABLE")
    group_style_dir: str = Field(
        default="data/dl_senpai/group_style", alias="DL_SENPAI_GROUP_STYLE_DIR"
    )
    group_style_top_n: int = Field(default=8, alias="DL_SENPAI_GROUP_STYLE_TOP_N")
    group_style_min_count: int = Field(default=3, alias="DL_SENPAI_GROUP_STYLE_MIN_COUNT")

    memory_recall_enable: bool = Field(
        default=True, alias="DL_SENPAI_MEMORY_RECALL_ENABLE"
    )
    memory_recent_turns: int = Field(default=24, alias="DL_SENPAI_MEMORY_RECENT_TURNS")
    # 近期历史里最多带几条她自己的回复。
    # 为什么要限：memory 是追加的，越早的 assistant 内容越可能是旧人设时期的
    # 输出（猫娘腔、判词腔、自我点评）。24 条全带上等于把旧毛病当范本反复示范。
    history_assistant_turns: int = Field(
        default=8, alias="DL_SENPAI_HISTORY_ASSISTANT_TURNS"
    )
    memory_recall_top: int = Field(default=2, alias="DL_SENPAI_MEMORY_RECALL_TOP")

    # -- 最近聊天缓冲（记录全部消息，含她没参与的）--
    # 与 ChatMemory 的区别：后者只记她回过的轮次，群里别人互聊的内容
    # 从不入库；她回「你怎么看」时需要的正是那段背景。
    recent_chat_enable: bool = Field(
        default=True, alias="DL_SENPAI_RECENT_CHAT_ENABLE"
    )
    recent_chat_dir: str = Field(
        default="data/dl_senpai/recent_chat", alias="DL_SENPAI_RECENT_CHAT_DIR"
    )
    # 保留时长（小时）。默认一整天：过一会就忘的窗口在群里不够用。
    recent_chat_ttl_hours: float = Field(
        default=24.0, alias="DL_SENPAI_RECENT_CHAT_TTL_HOURS"
    )
    # 注入 prompt 的条数。一天上千条不可能全塞，所以分两段取：
    # 最近 recent_n 条 + 当天更早的按关键词召回 recall_n 条。
    recent_chat_recent_n: int = Field(
        default=20, alias="DL_SENPAI_RECENT_CHAT_RECENT_N"
    )
    recent_chat_recall_n: int = Field(
        default=10, alias="DL_SENPAI_RECENT_CHAT_RECALL_N"
    )
    recent_chat_max_chars: int = Field(
        default=2000, alias="DL_SENPAI_RECENT_CHAT_MAX_CHARS"
    )

    sticker_enable: bool = Field(default=True, alias="DL_SENPAI_STICKER_ENABLE")
    sticker_collect: bool = Field(default=True, alias="DL_SENPAI_STICKER_COLLECT")
    sticker_dir: str = Field(default="data/dl_senpai/stickers", alias="DL_SENPAI_STICKER_DIR")
    sticker_max_per_session: int = Field(default=200, alias="DL_SENPAI_STICKER_MAX_PER_SESSION")
    sticker_reply_prob: float = Field(default=0.45, alias="DL_SENPAI_STICKER_REPLY_PROB")
    sticker_reply_prob_interrupt: float = Field(
        default=0.18, alias="DL_SENPAI_STICKER_REPLY_PROB_INTERRUPT"
    )

    vision_enable: bool = Field(default=True, alias="DL_SENPAI_VISION_ENABLE")
    vision_max_images: int = Field(default=3, alias="DL_SENPAI_VISION_MAX_IMAGES")
    image_interrupt_boost: float = Field(default=0.06, alias="DL_SENPAI_IMAGE_INTERRUPT_BOOST")

    # 引用图：对方「引用一张图」时本轮消息段里其实没有图（QQ 折成 [图片]），
    # 于是永远走不到视觉。默认开启回退，但加三道闸，并且都能一键关：
    #   same_sender_only —— 只认本人引用的图，以及引用学姐自己发过的图。
    #     反过来（谁都认）意味着任何人引用群友的图都会触发一次下载 + 视觉调用。
    #   ttl_seconds —— 引用几天前的图不该反复下载。
    #   allow_mface —— 动画表情不是「配图」，认它容易把引用个表情包当成带图提问。
    vision_quoted_enable: bool = Field(default=True, alias="DL_SENPAI_VISION_QUOTED_ENABLE")
    vision_quoted_same_sender_only: bool = Field(
        default=True, alias="DL_SENPAI_VISION_QUOTED_SAME_SENDER_ONLY"
    )
    vision_quoted_ttl_seconds: int = Field(
        default=120, alias="DL_SENPAI_VISION_QUOTED_TTL_SECONDS"
    )
    vision_quoted_allow_mface: bool = Field(
        default=False, alias="DL_SENPAI_VISION_QUOTED_ALLOW_MFACE"
    )

    # PPT 配图：复用已有的 AI 绘图后端（draw_* 那组配置），不额外引入图库 API。
    # 上限存在的意义是控制等待时间 —— 每张图要几十秒，不设限一份 deck 能等好几分钟。
    ppt_image_enable: bool = Field(default=True, alias="DL_SENPAI_PPT_IMAGE_ENABLE")
    ppt_image_max: int = Field(default=2, alias="DL_SENPAI_PPT_IMAGE_MAX")

    # 成品级 PPT 生成：按主题直接做出完整 deck，耗时几分钟，依赖本机常驻进程。
    # 留这个开关的理由：它一旦卡住会占住队列，出问题时要能一键停用而不必改代码重启。
    deck_enable: bool = Field(default=True, alias="DL_SENPAI_DECK_ENABLE")

    # 附件：有人发文件并唤醒时，自动读几个、单个最大多少 MB
    # 默认 2 个：一次丢十个 PDF 会把上下文吃爆，多的用 read_doc 手动读
    attachment_auto_read_max: int = Field(
        default=2, alias="DL_SENPAI_ATTACHMENT_AUTO_READ_MAX"
    )

    # 语音：edge=Edge TTS；gptsovits=本地 GPT-SoVITS API（可随 bot.py 自动拉起 api_v2）
    voice_enable: bool = Field(default=True, alias="DL_SENPAI_VOICE_ENABLE")
    voice_tts_provider: str = Field(default="gptsovits", alias="DL_SENPAI_VOICE_PROVIDER")
    voice_profile: str = Field(default="angelina", alias="DL_SENPAI_VOICE_PROFILE")
    voice_reply_prob: float = Field(default=0.12, alias="DL_SENPAI_VOICE_REPLY_PROB")
    voice_max_chars: int = Field(default=120, alias="DL_SENPAI_VOICE_MAX_CHARS")
    voice_name: str = Field(
        default="zh-CN-XiaoyiNeural", alias="DL_SENPAI_VOICE_NAME"
    )
    voice_rate: str = Field(default="+14%", alias="DL_SENPAI_VOICE_RATE")
    voice_pitch: str = Field(default="+8Hz", alias="DL_SENPAI_VOICE_PITCH")
    voice_dir: str = Field(default="data/dl_senpai/voice", alias="DL_SENPAI_VOICE_DIR")
    voice_allow_on_interrupt: bool = Field(
        default=False, alias="DL_SENPAI_VOICE_ALLOW_ON_INTERRUPT"
    )
    # 语音语言锁：非空（如 ja）时她**只会**用这门语言发语音条，朗读内容也会先换成这门语言。
    # 为什么要这把锁：GPT-SoVITS 的音色是拿日语参考音频调的，混进中文/英文出来既不是
    # 角色声线也听不懂；所以宁可不发这一条语音，也不发错语言的。
    voice_lang_lock: str = Field(default="", alias="DL_SENPAI_VOICE_LANG")

    # 撤回：她自己的消息被别人撤回时，允许她当场接一句（只在自己被撤时，且带冷却）。
    # 为什么加开关与冷却：撤回通知来得又密又碎（有人连撤十条），
    # 不限制的话群里会变成她一个人在追着每条撤回喊「怎么撤我」。
    recall_reply_enable: bool = Field(default=True, alias="DL_SENPAI_RECALL_REPLY_ENABLE")
    recall_reply_cooldown: int = Field(default=180, alias="DL_SENPAI_RECALL_REPLY_COOLDOWN")
    gptsovits_base_url: str = Field(
        default="http://127.0.0.1:9880", alias="DL_SENPAI_GPTSOVITS_URL"
    )
    gptsovits_ref_audio: str = Field(
        default="GPT-SoVITS-v2pro-20250604/angelina/ref.wav",
        alias="DL_SENPAI_GPTSOVITS_REF_AUDIO",
    )
    gptsovits_prompt_text: str = Field(
        default=(
            "あまりシラクーザ人っぽくないんだよね それに響も名前っぽくないってよく言われるから"
        ),
        alias="DL_SENPAI_GPTSOVITS_PROMPT_TEXT",
    )
    gptsovits_prompt_lang: str = Field(default="ja", alias="DL_SENPAI_GPTSOVITS_PROMPT_LANG")
    gptsovits_text_lang: str = Field(default="zh", alias="DL_SENPAI_GPTSOVITS_TEXT_LANG")
    gptsovits_text_split_method: str = Field(
        default="cut5", alias="DL_SENPAI_GPTSOVITS_TEXT_SPLIT_METHOD"
    )
    gptsovits_speed_factor: float = Field(
        default=1.0, alias="DL_SENPAI_GPTSOVITS_SPEED_FACTOR"
    )
    gptsovits_timeout: float = Field(default=120.0, alias="DL_SENPAI_GPTSOVITS_TIMEOUT")
    gptsovits_fallback_edge: bool = Field(
        default=False, alias="DL_SENPAI_GPTSOVITS_FALLBACK_EDGE"
    )
    gptsovits_autostart: bool = Field(default=True, alias="DL_SENPAI_GPTSOVITS_AUTOSTART")
    gptsovits_home: str = Field(
        default="GPT-SoVITS-v2pro-20250604", alias="DL_SENPAI_GPTSOVITS_HOME"
    )
    gptsovits_config: str = Field(
        default="GPT_SoVITS/configs/tts_infer.yaml",
        alias="DL_SENPAI_GPTSOVITS_CONFIG",
    )
    gptsovits_startup_timeout: float = Field(
        default=180.0, alias="DL_SENPAI_GPTSOVITS_STARTUP_TIMEOUT"
    )

    # 群名片：需机器人是群管；对方要求可改，聊到改名时可偶尔主动改
    card_enable: bool = Field(default=True, alias="DL_SENPAI_CARD_ENABLE")
    card_max_len: int = Field(default=16, alias="DL_SENPAI_CARD_MAX_LEN")
    card_playful_cooldown: int = Field(default=600, alias="DL_SENPAI_CARD_PLAYFUL_COOLDOWN")
    card_allow_on_interrupt: bool = Field(
        default=True, alias="DL_SENPAI_CARD_ALLOW_ON_INTERRUPT"
    )

    # 禁言：被惹急时可短时禁言普通成员；需机器人是群管
    mute_enable: bool = Field(default=True, alias="DL_SENPAI_MUTE_ENABLE")
    mute_min_sec: int = Field(default=30, alias="DL_SENPAI_MUTE_MIN_SEC")
    mute_max_sec: int = Field(default=120, alias="DL_SENPAI_MUTE_MAX_SEC")
    mute_cooldown: int = Field(default=180, alias="DL_SENPAI_MUTE_COOLDOWN")
    mute_allow_on_interrupt: bool = Field(
        default=False, alias="DL_SENPAI_MUTE_ALLOW_ON_INTERRUPT"
    )

    # 热梗 / 热榜：TTL 缓存后注入提示词；亦可给 Cursor MCP 复用
    trends_enable: bool = Field(default=True, alias="DL_SENPAI_TRENDS_ENABLE")
    trends_base_url: str = Field(
        default="https://uapis.cn/api/v1/misc/hotboard",
        alias="DL_SENPAI_TRENDS_BASE_URL",
    )
    trends_fallback_bases: str = Field(
        default="https://api-hot.imsyy.top",
        alias="DL_SENPAI_TRENDS_FALLBACK_BASES",
    )
    trends_sources: str = Field(
        default="weibo,douyin,bilibili,baidu,zhihu",
        alias="DL_SENPAI_TRENDS_SOURCES",
    )
    trends_ttl_sec: int = Field(default=1800, alias="DL_SENPAI_TRENDS_TTL_SEC")
    trends_per_source: int = Field(default=8, alias="DL_SENPAI_TRENDS_PER_SOURCE")
    trends_max_items: int = Field(default=20, alias="DL_SENPAI_TRENDS_MAX_ITEMS")
    trends_prompt_max_chars: int = Field(
        default=800, alias="DL_SENPAI_TRENDS_PROMPT_MAX_CHARS"
    )
    trends_cache_dir: str = Field(
        default="data/dl_senpai/trends", alias="DL_SENPAI_TRENDS_CACHE_DIR"
    )

    # 联网检索：模型写 <<<SEARCH query>>> 后本地实时检索再续答；亦可给 Cursor MCP 复用
    # 读群公告 / 群文件列表 / 群相册（只读，无群内副作用）
    group_read_enable: bool = Field(default=True, alias="DL_SENPAI_GROUP_READ_ENABLE")
    search_enable: bool = Field(default=True, alias="DL_SENPAI_SEARCH_ENABLE")
    search_auto: bool = Field(
        default=True,
        alias="DL_SENPAI_SEARCH_AUTO",
        description="时效/事实类问题在调用模型前自动联网，无需 <<<SEARCH>>>",
    )
    search_auto_aggressive: bool = Field(
        default=False,
        alias="DL_SENPAI_SEARCH_AUTO_AGGRESSIVE",
        description=(
            "对像问事实/背景的消息更积极自动联网（类似网页版助手）。"
            "默认关：2026-10-24 实测它把中文闲聊也判成事实查询，"
            "三句日常对话各触发 5 轮无关检索；时效词/热点词/明确要求搜索仍照搜。"
        ),
    )
    search_provider: str = Field(default="auto", alias="DL_SENPAI_SEARCH_PROVIDER")
    search_max_results: int = Field(default=5, alias="DL_SENPAI_SEARCH_MAX_RESULTS")
    search_max_rounds: int = Field(default=3, alias="DL_SENPAI_SEARCH_MAX_ROUNDS")
    search_max_auto_queries: int = Field(default=4, alias="DL_SENPAI_SEARCH_MAX_AUTO_QUERIES")
    search_timeout: float = Field(default=12.0, alias="DL_SENPAI_SEARCH_TIMEOUT")
    # 整次联网检索的总预算（首轮 + 换词重试 + 时效补搜 + 抓正文）。
    # 调用方没有外层超时，不设这个上限时实测能拖到 25 秒。
    search_deadline: float = Field(default=12.0, alias="DL_SENPAI_SEARCH_DEADLINE")

    # 静态抓不到正文时是否开浏览器兜底（知乎/贴吧/B站 挡爬虫或是 SPA）
    search_browser_fallback: bool = Field(
        default=True, alias="DL_SENPAI_SEARCH_BROWSER_FALLBACK"
    )
    search_region: str = Field(default="zh-cn", alias="DL_SENPAI_SEARCH_REGION")
    search_prompt_max_chars: int = Field(
        default=2400, alias="DL_SENPAI_SEARCH_PROMPT_MAX_CHARS"
    )
    search_allow_on_interrupt: bool = Field(
        default=False, alias="DL_SENPAI_SEARCH_ALLOW_ON_INTERRUPT"
    )
    search_searxng_url: str = Field(default="", alias="DL_SENPAI_SEARCH_SEARXNG_URL")
    search_tavily_api_key: str = Field(default="", alias="DL_SENPAI_SEARCH_TAVILY_API_KEY")
    # 少于这么多个独立域名时，检索结果里会明确标注「未获多方印证」。
    # 默认 2：一条来源只能算线索，不能当定论 —— 这是「搜到一个就认可」的闸门。
    search_min_independent_domains: int = Field(
        default=2,
        alias="DL_SENPAI_SEARCH_MIN_INDEPENDENT_DOMAINS",
        description="联网结果至少要有几个独立域名才算获得交叉印证",
    )
    # 抓网页正文：检索摘要常常只有标题级信息（"XX官网首页"），
    # 只给标题等于逼模型照着标题编，或者干脆说"只搜到官网"。
    search_fetch_page_body: bool = Field(
        default=True,
        alias="DL_SENPAI_SEARCH_FETCH_PAGE_BODY",
        description="是否给前几条结果抓网页正文摘录",
    )
    search_page_body_timeout: float = Field(
        default=6.0, alias="DL_SENPAI_SEARCH_PAGE_BODY_TIMEOUT"
    )
    search_page_body_max_chars: int = Field(
        default=500, alias="DL_SENPAI_SEARCH_PAGE_BODY_MAX_CHARS"
    )

    # 生图：OpenAI 兼容 images API（默认复用中转 Key/地址）
    draw_enable: bool = Field(default=True, alias="DL_SENPAI_DRAW_ENABLE")
    draw_model: str = Field(default="gpt-image-1", alias="DL_SENPAI_DRAW_MODEL")
    draw_api_key: str = Field(default="", alias="DL_SENPAI_DRAW_API_KEY")
    draw_base_url: str = Field(default="", alias="DL_SENPAI_DRAW_BASE_URL")
    draw_size: str = Field(default="1024x1024", alias="DL_SENPAI_DRAW_SIZE")
    draw_quality: str = Field(default="", alias="DL_SENPAI_DRAW_QUALITY")
    draw_timeout: float = Field(default=120.0, alias="DL_SENPAI_DRAW_TIMEOUT")
    draw_cooldown: int = Field(default=0, alias="DL_SENPAI_DRAW_COOLDOWN")
    draw_max_prompt_chars: int = Field(default=800, alias="DL_SENPAI_DRAW_MAX_PROMPT_CHARS")
    # 画质增强三件套（对应网页版那套「内部改写 + 自检」体验）：
    # 本中转的 gpt-image-* 实测 revised_prompt 原样返回，完全不做提示词改写，
    # 也不会自己检查画得对不对，所以在外面补一层。
    draw_enhance: bool = Field(
        default=True,
        alias="DL_SENPAI_DRAW_ENHANCE",
        description="生图前用视觉模型扩写提示词 / 转述参考图",
    )
    draw_verify: bool = Field(
        default=True,
        alias="DL_SENPAI_DRAW_VERIFY",
        description="生图后用视觉模型自检，不合格带反馈重画",
    )
    draw_vision_model: str = Field(
        default="glm-5.3",
        alias="DL_SENPAI_DRAW_VISION_MODEL",
        description="增强/自检用的视觉模型（实测 deepseek-v4.1-flash 看不了图）",
    )
    draw_max_retry: int = Field(
        default=1,
        alias="DL_SENPAI_DRAW_MAX_RETRY",
        description="自检不合格时最多重画几次（1 次约 +27s）",
    )
    draw_max_archive: int = Field(default=200, alias="DL_SENPAI_DRAW_MAX_ARCHIVE")
    draw_dir: str = Field(default="data/dl_senpai/draw", alias="DL_SENPAI_DRAW_DIR")
    draw_allow_on_interrupt: bool = Field(
        default=False, alias="DL_SENPAI_DRAW_ALLOW_ON_INTERRUPT"
    )

    # 浏览器逛网站：Playwright 截图 / 淘宝搜商品 / Pixiv 搜图
    browser_enable: bool = Field(default=True, alias="DL_SENPAI_BROWSER_ENABLE")
    browser_dir: str = Field(default="data/dl_senpai/browser", alias="DL_SENPAI_BROWSER_DIR")
    browser_timeout: float = Field(default=90.0, alias="DL_SENPAI_BROWSER_TIMEOUT")
    browser_cooldown: int = Field(default=30, alias="DL_SENPAI_BROWSER_COOLDOWN")
    browser_max_images: int = Field(default=3, alias="DL_SENPAI_BROWSER_MAX_IMAGES")
    browser_headless: bool = Field(default=True, alias="DL_SENPAI_BROWSER_HEADLESS")
    browser_viewport_width: int = Field(default=1280, alias="DL_SENPAI_BROWSER_VIEWPORT_WIDTH")
    browser_viewport_height: int = Field(default=900, alias="DL_SENPAI_BROWSER_VIEWPORT_HEIGHT")
    browser_allowed_domains: str = Field(
        default="",
        alias="DL_SENPAI_BROWSER_ALLOWED_DOMAINS",
        description="截图 URL 域名白名单，逗号分隔；空=内置常用站；*=全部",
    )
    browser_allow_on_interrupt: bool = Field(
        default=False, alias="DL_SENPAI_BROWSER_ALLOW_ON_INTERRUPT"
    )

    # 知识库：外部 RAGFlow 检索。
    #
    # 默认**关闭**：knowledge 工具一旦可见，模型每轮都要多花一次「要不要查资料」
    # 的判断；没配好地址/密钥时更会直接报错，所以要求显式开启。
    knowledge_enable: bool = Field(
        default=False,
        alias="DL_SENPAI_KNOWLEDGE_ENABLE",
        description="开启后把检索工具暴露给模型，由模型自行决定何时查资料",
    )
    # 自动把知识库结果和联网结果一起塞进同一次提问 = 混合检索。
    # 默认 True：用户要的是「两种结果混合」，而不是指望模型自己想起来查库。
    # 留开关是因为它会给每条事实类消息多加一次检索耗时。
    knowledge_auto: bool = Field(
        default=True,
        alias="DL_SENPAI_KNOWLEDGE_AUTO",
        description="事实类问题自动查知识库，与联网结果混合注入",
    )
    knowledge_provider: str = Field(default="ragflow", alias="DL_SENPAI_KNOWLEDGE_PROVIDER")
    knowledge_base_url: str = Field(
        default="",
        alias="DL_SENPAI_KNOWLEDGE_BASE_URL",
        description="RAGFlow 地址，需带部署前缀，如 http://10.0.0.1:31173/ontoweb-ragflow",
    )
    # 以 _API_KEY 结尾 → 命中后台写入黑名单，只能在 .env 手工改（有意的安全取舍）
    knowledge_api_key: str = Field(default="", alias="DL_SENPAI_KNOWLEDGE_API_KEY")
    knowledge_dataset_ids: str = Field(
        default="",
        alias="DL_SENPAI_KNOWLEDGE_DATASET_IDS",
        description="逗号分隔的 dataset id；留空=由服务端在该 key 可见范围内检索",
    )
    # 领域闸门：查询没点名本地知识库里任何领域时，直接返回空。
    # 关掉就退回旧行为（靠分数硬塞，实测会塞进无关领域的内容）。
    knowledge_local_require_domain: bool = Field(
        default=True, alias="DL_SENPAI_KNOWLEDGE_LOCAL_REQUIRE_DOMAIN"
    )
    knowledge_relevance_gate: bool = Field(
        default=True, alias="DL_SENPAI_KNOWLEDGE_RELEVANCE_GATE"
    )
    knowledge_high_confidence_score: float = Field(
        default=0.75, alias="DL_SENPAI_KNOWLEDGE_HIGH_CONFIDENCE_SCORE"
    )
    knowledge_top_k: int = Field(default=5, alias="DL_SENPAI_KNOWLEDGE_TOP_K")
    knowledge_similarity_threshold: float = Field(
        default=0.2, alias="DL_SENPAI_KNOWLEDGE_SIMILARITY_THRESHOLD"
    )
    knowledge_vector_weight: float = Field(
        default=0.3, alias="DL_SENPAI_KNOWLEDGE_VECTOR_WEIGHT"
    )
    knowledge_timeout: float = Field(default=8.0, alias="DL_SENPAI_KNOWLEDGE_TIMEOUT")
    knowledge_max_chunks: int = Field(default=5, alias="DL_SENPAI_KNOWLEDGE_MAX_CHUNKS")
    knowledge_max_chars: int = Field(
        default=3000,
        alias="DL_SENPAI_KNOWLEDGE_MAX_CHARS",
        description="检索结果注入提示词的总字符预算",
    )
    knowledge_prompt_header: str = Field(
        default="【知识库检索结果】",
        alias="DL_SENPAI_KNOWLEDGE_PROMPT_HEADER",
    )
    # 本地 Markdown 知识库。与 RAGFlow **互相独立**：
    # 它不需要地址/密钥，服务端挂了也照常工作，所以单独一个开关，
    # 而不是挂在 knowledge_enable（那个是"把检索工具暴露给模型"）。
    knowledge_local_enable: bool = Field(
        default=True,
        alias="DL_SENPAI_KNOWLEDGE_LOCAL_ENABLE",
        description="启用本地 Markdown 知识库（knowledge_base 目录）",
    )
    knowledge_local_dir: str = Field(
        default="knowledge_base",
        alias="DL_SENPAI_KNOWLEDGE_LOCAL_DIR",
        description="本地知识库目录；相对路径按项目根目录解析",
    )

    # MCP 接入：把**外部** MCP Server 的工具导入给 Agent。
    #
    # 与仓库里的 mcp_servers/ 方向相反：那些是「把学姐的能力导出给 Cursor」，
    # 这里是「让学姐去用别人的工具」。两者共用业务模块，不互相替代。
    #
    # 默认**关闭**：它会拉起第三方进程或连外部地址，属于必须显式选择的能力。
    # 另外它只对原生 tool calling 有意义（见 mcp_setup 的 blocked 分支）。
    mcp_enable: bool = Field(
        default=False,
        alias="DL_SENPAI_MCP_ENABLE",
        description="接入外部 MCP Server 的工具（需同时开启 AGENT_NATIVE_TOOLS）",
    )
    mcp_servers: str = Field(
        default="",
        alias="DL_SENPAI_MCP_SERVERS",
        description="MCP 服务器配置 JSON；兼容带 mcpServers 外壳的写法",
    )
    mcp_config_path: str = Field(
        default="",
        alias="DL_SENPAI_MCP_CONFIG",
        description="从 JSON 文件读 MCP 配置（与 DL_SENPAI_MCP_SERVERS 二选一）",
    )
    mcp_init_timeout: float = Field(default=30.0, alias="DL_SENPAI_MCP_INIT_TIMEOUT")
    mcp_tool_timeout: float = Field(default=30.0, alias="DL_SENPAI_MCP_TOOL_TIMEOUT")
    mcp_max_tools: int = Field(
        default=40,
        alias="DL_SENPAI_MCP_MAX_TOOLS",
        description="所有 MCP 服务器合计最多注册多少工具（0=不限），防上下文被灌爆",
    )
    mcp_tools_admin_only: bool = Field(
        default=True,
        alias="DL_SENPAI_MCP_TOOLS_ADMIN_ONLY",
        description="MCP 工具只对管理员可见；关掉则对所有群友开放",
    )
    mcp_disabled_servers: str = Field(
        default="",
        alias="DL_SENPAI_MCP_DISABLED_SERVERS",
        description="逗号分隔的服务器名，运行期单独屏蔽（不必改整段 JSON）",
    )

    # 社交：自动同意好友 / 拉群邀请（入群申请永不自动同意，留给真人群管）
    auto_accept_friend: bool = Field(default=True, alias="DL_SENPAI_AUTO_ACCEPT_FRIEND")
    auto_accept_group: bool = Field(
        default=True,
        alias="DL_SENPAI_AUTO_ACCEPT_GROUP",
        description="自动同意「拉学姐进群」邀请；不含别人申请进群（add）",
    )
    auto_accept_friend_remark: str = Field(
        default="", alias="DL_SENPAI_AUTO_ACCEPT_FRIEND_REMARK"
    )

    # 签到：默认仅指定群启用
    checkin_enable: bool = Field(default=True, alias="DL_SENPAI_CHECKIN_ENABLE")
    # 默认留空：群号写死在源码里，一旦仓库开源就跟着出去了
    # （scripts/check_leaks.py 专门扫这类东西）。实际值放 .env，.env 已被 git 忽略。
    # 留空 = 没有群开启签到，不是错误状态；要用的群在 .env 里配上即可。
    checkin_groups: str = Field(default="", alias="DL_SENPAI_CHECKIN_GROUPS")
    checkin_dir: str = Field(default="data/dl_senpai/checkin", alias="DL_SENPAI_CHECKIN_DIR")
    checkin_sticker_prob: float = Field(default=0.35, alias="DL_SENPAI_CHECKIN_STICKER_PROB")
    checkin_sync_special_title: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "DL_SENPAI_CHECKIN_SYNC_SPECIAL_TITLE",
            "DL_SENPAI_CHECKIN_SYNC_TITLE_CARD",
        ),
    )

    # 长时印象：按 QQ 号跨群记住每个人的特征
    person_memory_enable: bool = Field(
        default=True, alias="DL_SENPAI_PERSON_MEMORY_ENABLE"
    )
    person_memory_dir: str = Field(
        default="data/dl_senpai/person_memory",
        alias="DL_SENPAI_PERSON_MEMORY_DIR",
    )
    person_memory_max_traits: int = Field(
        default=16, alias="DL_SENPAI_PERSON_MEMORY_MAX_TRAITS"
    )
    person_memory_max_note_chars: int = Field(
        default=400, alias="DL_SENPAI_PERSON_MEMORY_MAX_NOTE_CHARS"
    )
    person_memory_allow_on_interrupt: bool = Field(
        default=False,
        alias="DL_SENPAI_PERSON_MEMORY_ALLOW_ON_INTERRUPT",
        description="插嘴时是否允许写入印象（注入印象默认开）",
    )

    # 好感度（与签到群共用白名单）
    affection_enable: bool = Field(default=True, alias="DL_SENPAI_AFFECTION_ENABLE")
    affection_dir: str = Field(
        default="data/dl_senpai/affection", alias="DL_SENPAI_AFFECTION_DIR"
    )
    affection_gain_checkin: int = Field(default=5, alias="DL_SENPAI_AFFECTION_GAIN_CHECKIN")
    affection_gain_task: int = Field(default=3, alias="DL_SENPAI_AFFECTION_GAIN_TASK")
    affection_gain_chat: int = Field(default=2, alias="DL_SENPAI_AFFECTION_GAIN_CHAT")
    affection_gain_shop_buy: int = Field(
        default=10, alias="DL_SENPAI_AFFECTION_GAIN_SHOP_BUY"
    )
    affection_gain_shop_wear: int = Field(
        default=3, alias="DL_SENPAI_AFFECTION_GAIN_SHOP_WEAR"
    )
    affection_sync_special_title: bool = Field(
        default=True, alias="DL_SENPAI_AFFECTION_SYNC_SPECIAL_TITLE"
    )
    group_title_fallback_card: bool = Field(
        default=False, alias="DL_SENPAI_GROUP_TITLE_FALLBACK_CARD"
    )

    # 积分服装店
    shop_enable: bool = Field(default=True, alias="DL_SENPAI_SHOP_ENABLE")
    shop_dir: str = Field(default="data/dl_senpai/shop", alias="DL_SENPAI_SHOP_DIR")
    shop_catalog_path: str = Field(
        default="data/dl_senpai/shop/catalog.json",
        alias="DL_SENPAI_SHOP_CATALOG",
    )
    shop_auto_import_enable: bool = Field(default=True, alias="DL_SENPAI_SHOP_AUTO_IMPORT")
    shop_auto_import_queries: str = Field(
        default="洛丽塔连衣裙,JK制服女装,汉服连衣裙,女仆装,哥特萝莉裙,和服女装,泳装",
        alias="DL_SENPAI_SHOP_AUTO_IMPORT_QUERIES",
    )
    shop_auto_import_per_run: int = Field(default=2, alias="DL_SENPAI_SHOP_AUTO_IMPORT_PER_RUN")
    shop_auto_import_cooldown: int = Field(
        default=300,
        alias="DL_SENPAI_SHOP_AUTO_IMPORT_COOLDOWN",
    )
    shop_daily_refresh_hour: int = Field(default=4, alias="DL_SENPAI_SHOP_DAILY_REFRESH_HOUR")
    shop_affection_discount_enable: bool = Field(
        default=True, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_ENABLE"
    )
    shop_affection_discount_base_chance: float = Field(
        default=0.05, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_BASE_CHANCE"
    )
    shop_affection_discount_chance_per_tier: float = Field(
        default=0.08, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_CHANCE_PER_TIER"
    )
    shop_affection_discount_max_chance: float = Field(
        default=0.75, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_MAX_CHANCE"
    )
    shop_affection_discount_base_rate: float = Field(
        default=0.05, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_BASE_RATE"
    )
    shop_affection_discount_rate_per_tier: float = Field(
        default=0.03, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_RATE_PER_TIER"
    )
    shop_affection_discount_max_rate: float = Field(
        default=0.35, alias="DL_SENPAI_SHOP_AFFECTION_DISCOUNT_MAX_RATE"
    )

    # 商店缩略图并发生成数：单张 gpt-image-2 约 85 秒，串行 4 件要 4 分半；
    # 并发能压到 ~90 秒，但上游有限流，所以做成可调（1=退回串行）。
    shop_thumb_concurrency: int = Field(
        default=3, alias="DL_SENPAI_SHOP_THUMB_CONCURRENCY"
    )
    # 后台预热：每日 4 点换新后素材是冷的，先在后台把各群缩略图生成好，
    # 用户白天问「商店」就能秒回；关掉则退回「首次访问现画」。
    shop_warmup: bool = Field(default=True, alias="DL_SENPAI_SHOP_WARMUP")



    # ================================================================
    # 以下这批是「借鉴小黑盒 @异世回忆（小鲸鱼）那套 kit」落地到学姐的部分。
    # 统一原则：**默认值一律取「什么都不做」**。
    # 这套能力的价值在打开之后像个人，但其中好几项能发出撤不回的消息、
    # 或者动到桌面，所以宁可让用户手动打开，也不要在升级时自动生效。
    # ================================================================

    # -- 她自己过日子：随机间隔 / 真发概率 / 安静门槛 --
    # 原版是固定 9000 秒一跳。作者后来自己推翻了这个设计：「固定间隔 =
    # 闹钟，随机才像想起来才找你」。所以这里改成区间，每次醒来重新抽一个
    # 下一次的时间点（写进 state），而不是「到点就必醒」。
    own_life_interval_min_sec: int = Field(
        default=2700, alias="DL_SENPAI_OWN_LIFE_INTERVAL_MIN_SEC"
    )
    own_life_interval_max_sec: int = Field(
        default=6600, alias="DL_SENPAI_OWN_LIFE_INTERVAL_MAX_SEC"
    )
    # 被叫醒不等于要说话。决定「说话」之后还要再过一次骰子，
    # 否则每次醒来都冒一句，一天就是六条群消息（原版给的参考值 0.35）。
    own_life_send_probability: float = Field(
        default=0.35, alias="DL_SENPAI_OWN_LIFE_SEND_PROBABILITY"
    )
    # 你们正在聊的时候她不打断：主人最近 N 秒内说过话就不叫醒。
    own_life_min_silence_sec: int = Field(
        default=900, alias="DL_SENPAI_OWN_LIFE_MIN_SILENCE_SEC"
    )

    # -- 断线补课 --
    # 桥接断开期间的消息是真的丢了（不会补发）。重连后拉一次最近历史补上，
    # 但**只总结、不逐条回**——原版第一版就是开机后一口气回了 30 条。
    backfill_enable: bool = Field(default=False, alias="DL_SENPAI_BACKFILL_ENABLE")
    backfill_max_messages: int = Field(
        default=30, alias="DL_SENPAI_BACKFILL_MAX_MESSAGES"
    )
    backfill_max_age_sec: int = Field(
        default=21600, alias="DL_SENPAI_BACKFILL_MAX_AGE_SEC"
    )

    # -- 语音转文字（本地 whisper）--
    # 只做本地：她听到的语音不该上传到别人服务器（原版强调这一点）。
    asr_enable: bool = Field(default=False, alias="DL_SENPAI_ASR_ENABLE")
    asr_model: str = Field(default="small", alias="DL_SENPAI_ASR_MODEL")
    asr_language: str = Field(default="zh", alias="DL_SENPAI_ASR_LANGUAGE")
    asr_timeout_sec: float = Field(default=90.0, alias="DL_SENPAI_ASR_TIMEOUT_SEC")

    # -- 自选相册（她自己挑的图 + 一句理由）--
    # 与 draw_store（生成台账）不同：这里是「她挑的」，价值在挑这个动作。
    gallery_enable: bool = Field(default=False, alias="DL_SENPAI_GALLERY_ENABLE")
    gallery_dir: str = Field(
        default="data/dl_senpai/gallery", alias="DL_SENPAI_GALLERY_DIR"
    )
    gallery_max_items: int = Field(default=200, alias="DL_SENPAI_GALLERY_MAX_ITEMS")
    gallery_context_n: int = Field(default=3, alias="DL_SENPAI_GALLERY_CONTEXT_N")

    # -- 印象保鲜 --
    # 人会变。印象不重写，她三个月后还在用第一次见面的判断跟你说话。
    impression_refresh_enable: bool = Field(
        default=True, alias="DL_SENPAI_IMPRESSION_REFRESH_ENABLE"
    )
    impression_refresh_days: int = Field(
        default=14, alias="DL_SENPAI_IMPRESSION_REFRESH_DAYS"
    )

    # -- 周报 --
    weekly_enable: bool = Field(default=False, alias="DL_SENPAI_WEEKLY_ENABLE")
    weekly_weekday: int = Field(default=1, alias="DL_SENPAI_WEEKLY_WEEKDAY")
    weekly_hour: int = Field(default=22, alias="DL_SENPAI_WEEKLY_HOUR")
    weekly_groups: str = Field(default="", alias="DL_SENPAI_WEEKLY_GROUPS")

    # -- 问候去重 --
    # 「别每次念同一句开场白」是原版最实用的一条心得：固定问候听三天就背下来了。
    greeting_dedupe_enable: bool = Field(
        default=True, alias="DL_SENPAI_GREETING_DEDUPE_ENABLE"
    )
    greeting_dedupe_window_sec: int = Field(
        default=21600, alias="DL_SENPAI_GREETING_DEDUPE_WINDOW_SEC"
    )

    # -- 电脑（GUI）操作：默认关，围栏见 computer_control.py --
    computer_enable: bool = Field(default=False, alias="DL_SENPAI_COMPUTER_ENABLE")
    # view = 只能看（截图/窗口）；control = 能点能打字。升档要显式配。
    computer_mode: str = Field(default="view", alias="DL_SENPAI_COMPUTER_MODE")
    # 前台窗口白名单。**留空 = 谁都不许碰**（安全侧默认，不是便利侧默认）。
    computer_allow_apps: str = Field(default="", alias="DL_SENPAI_COMPUTER_ALLOW_APPS")
    computer_deny_keywords: str = Field(
        default="", alias="DL_SENPAI_COMPUTER_DENY_KEYWORDS"
    )
    # 是否必须持有租约才能操作。关掉它等于「只要白名单命中就能动」，
    # 只建议在调试期短暂关闭。
    computer_require_lease: bool = Field(
        default=True, alias="DL_SENPAI_COMPUTER_REQUIRE_LEASE"
    )
    computer_dir: str = Field(
        default="data/dl_senpai/computer", alias="DL_SENPAI_COMPUTER_DIR"
    )
    computer_max_actions_per_min: int = Field(
        default=20, alias="DL_SENPAI_COMPUTER_MAX_ACTIONS_PER_MIN"
    )
    computer_click_min_interval_ms: int = Field(
        default=300, alias="DL_SENPAI_COMPUTER_CLICK_MIN_INTERVAL_MS"
    )
    computer_screenshot_max_side: int = Field(
        default=1280, alias="DL_SENPAI_COMPUTER_SCREENSHOT_MAX_SIDE"
    )

    # ---- 读链接（把对方贴的 URL 变成正文）----
    # 只读能力，默认开：不开的话她看到链接只能靠截图或凭 URL 猜。
    link_read_enable: bool = Field(default=True, alias="DL_SENPAI_LINK_READ_ENABLE")
    # 主动读：消息里出现链接就先读一遍再让她开口。
    # 关掉之后只剩她自己想起 <<<READ url>>> 才读（更省，但群里常见的
    # 「甩个链接+一句话」大概率就答歪了）。
    link_read_auto: bool = Field(default=True, alias="DL_SENPAI_LINK_READ_AUTO")
    # 静态 HTML 抽不出正文时是否开浏览器渲染（SPA 兜底）。
    # 关掉能省内存，但知乎/小黑盒/B站专栏这类就只剩空壳。
    link_read_use_browser: bool = Field(default=True, alias="DL_SENPAI_LINK_READ_USE_BROWSER")
    # 浏览器渲染时"正文够了没"的门槛：低于这个字数就继续等（小黑盒这类站
    # 的正文是 XHR 后才挂上 DOM 的，等太短只能拿到导航和备案号）。
    link_read_browser_min_chars: int = Field(
        default=400, alias="DL_SENPAI_LINK_READ_BROWSER_MIN_CHARS"
    )
    # 喂给模型的正文上限（字）。太长会稀释注意力，超出部分首尾各留一半。
    link_read_max_chars: int = Field(default=6000, alias="DL_SENPAI_LINK_READ_MAX_CHARS")
    # 下载上限：超过就明确失败，不硬撑（大于 2MB 的"网页"基本是附件）。
    link_read_max_bytes: int = Field(default=2097152, alias="DL_SENPAI_LINK_READ_MAX_BYTES")
    link_read_timeout: float = Field(default=20.0, alias="DL_SENPAI_LINK_READ_TIMEOUT")

    # ---- 技能层（Skills：按需注入的一页领域说明）----
    # 定位见 skills.py 顶部：技能只是"说明书"，不执行任何东西。
    # 默认关：新能力先让想用的人显式打开，避免默认行为变化。
    skill_enable: bool = Field(default=False, alias="DL_SENPAI_SKILL_ENABLE")
    # 技能目录。相对路径按项目根解析（与 knowledge_base 等保持一致）。
    skill_dir: str = Field(default="data/skills", alias="DL_SENPAI_SKILL_DIR")
    # 单条技能正文注入上限：太长会稀释注意力，也会抢人设的戏份。
    skill_max_chars: int = Field(default=1500, alias="DL_SENPAI_SKILL_MAX_CHARS")
    # 单条消息最多注入几个技能。默认 1：两个领域规范同时上，模型会互相打架。
    skill_max_skills: int = Field(default=1, alias="DL_SENPAI_SKILL_MAX_SKILLS")
    # 热重载：改完 SKILL.md 立刻生效，不必重启 bot。
    # 调提示词本来就是"改一句、问一句"，重启一次十几秒，这个开关值这个代价。
    skill_hot_reload: bool = Field(default=True, alias="DL_SENPAI_SKILL_HOT_RELOAD")
    # 缓存时长：同一链接反复被贴时不必反复抓（新闻类可调小）。
    link_read_cache_ttl_sec: int = Field(default=21600, alias="DL_SENPAI_LINK_READ_CACHE_TTL_SEC")
    link_read_cache_dir: str = Field(default="", alias="DL_SENPAI_LINK_READ_CACHE_DIR")
    # 域名白名单，逗号分隔。**留空 = 除内网外都可以**（内网永远禁，见 link_reader）。
    link_read_allowed_domains: str = Field(
        default="", alias="DL_SENPAI_LINK_READ_ALLOWED_DOMAINS"
    )
    # 长文先摘要：超过下面的字数先压成要点，再连同原文片段一起给模型。
    link_read_summary_enable: bool = Field(
        default=True, alias="DL_SENPAI_LINK_READ_SUMMARY_ENABLE"
    )
    link_read_summary_min_chars: int = Field(
        default=4000, alias="DL_SENPAI_LINK_READ_SUMMARY_MIN_CHARS"
    )

    # ---- B站 / 小黑盒：只读上网（刷视频、搜帖子）----
    # 为什么默认开：这两种动作只读、不需要登录，出事的最小损失是"被站点限流"，
    # 不是"以她的名义发东西"。写入类的动作（发说说、发帖）另外一套开关，见 T2。
    bili_enable: bool = Field(default=True, alias="DL_SENPAI_BILI_ENABLE")
    # 每日配额：防她（或被群友指挥着）连着刷。0 或负数 = 不限。
    bili_daily_limit: int = Field(default=60, alias="DL_SENPAI_BILI_DAILY_LIMIT")
    bili_timeout: float = Field(default=15.0, alias="DL_SENPAI_BILI_TIMEOUT")
    heybox_enable: bool = Field(default=True, alias="DL_SENPAI_HEYBOX_ENABLE")
    heybox_daily_limit: int = Field(default=40, alias="DL_SENPAI_HEYBOX_DAILY_LIMIT")
    # 要用真浏览器渲染搜索页，比接口慢得多，超时给宽一点
    heybox_timeout: float = Field(default=30.0, alias="DL_SENPAI_HEYBOX_TIMEOUT")
    # 配额账本：B站/小黑盒/以后的动作类功能共用一份，按天清零
    quota_file: str = Field(default="data/dl_senpai/quota.json", alias="DL_SENPAI_QUOTA_FILE")

    # ---- 写入层（T2）：发说说、黑盒评论 ----
    # 默认全关。理由见 docs/social-web.md：这两个是「以她名义在公开平台留痕」，
    # 发出去撤不回来，跟只读层不是一个量级。
    qzone_enable: bool = Field(default=False, alias="DL_SENPAI_QZONE_ENABLE")
    # 是否允许她自己直接发（false = 只存草稿，等主人说「发」）
    qzone_auto_post: bool = Field(default=False, alias="DL_SENPAI_QZONE_AUTO_POST")
    qzone_daily_limit: int = Field(default=1, alias="DL_SENPAI_QZONE_DAILY_LIMIT")
    qzone_max_chars: int = Field(default=120, alias="DL_SENPAI_QZONE_MAX_CHARS")
    qzone_draft_file: str = Field(
        default="data/dl_senpai/qzone_drafts.json", alias="DL_SENPAI_QZONE_DRAFT_FILE"
    )
    heybox_write_enable: bool = Field(
        default=False, alias="DL_SENPAI_HEYBOX_WRITE_ENABLE"
    )
    # 0 = 不设上限。为什么不按天卡死：主人要的是"她想评就评"，
    # 卡条数会让她攒着补发，反而更像机器人。真正的护栏是下面的间隔和去重。
    heybox_comment_daily_limit: int = Field(
        default=0, alias="DL_SENPAI_HEYBOX_COMMENT_DAILY_LIMIT"
    )
    # 两条评论之间的最小间隔（秒）。默认 10 分钟，评太快是最容易被判脚本的信号。
    heybox_comment_min_gap_sec: int = Field(
        default=600, alias="DL_SENPAI_HEYBOX_COMMENT_MIN_GAP_SEC"
    )
    # 评过的帖子、最近的文案，存这里（台账 + 去重依据）
    heybox_write_state_file: str = Field(
        default="data/dl_senpai/heybox_write_state.json",
        alias="DL_SENPAI_HEYBOX_WRITE_STATE_FILE",
    )
    # ---- 自己刷帖子（自主活动）----
    # 开了之后她自己会隔一阵去刷首页信息流，遇到想说的就评一句。
    heybox_browse_enable: bool = Field(default=False, alias="DL_SENPAI_HEYBOX_BROWSE_ENABLE")
    heybox_browse_interval_min_sec: int = Field(
        default=3600, alias="DL_SENPAI_HEYBOX_BROWSE_INTERVAL_MIN_SEC"
    )
    heybox_browse_interval_max_sec: int = Field(
        default=10800, alias="DL_SENPAI_HEYBOX_BROWSE_INTERVAL_MAX_SEC"
    )
    # 刷到想说的东西之后，真正评论的概率（0~1）。别调到 1：全都评等于刷屏。
    heybox_browse_comment_probability: float = Field(
        default=0.35, alias="DL_SENPAI_HEYBOX_BROWSE_COMMENT_PROBABILITY"
    )
    # 两次"开浏览器"之间的最小间隔。太密会被小黑盒判成脚本（实测 show_captcha）
    heybox_session_gap_sec: int = Field(default=240, alias="DL_SENPAI_HEYBOX_SESSION_GAP_SEC")
    # 别人回她的留言，她要不要自己接话（默认接：留言是对话，不是刷屏）
    heybox_browse_reply_enable: bool = Field(
        default=True, alias="DL_SENPAI_HEYBOX_BROWSE_REPLY_ENABLE"
    )
    # 一次醒来最多回头看几篇帖子找留言（每篇要开一次浏览器，别设太大）
    heybox_inbox_posts: int = Field(default=1, alias="DL_SENPAI_HEYBOX_INBOX_POSTS")
    # ---- B站（小号登录后才能用）----
    # 发评论/回留言总开关。默认关：没有小号登录态时它只会报错。
    bili_write_enable: bool = Field(default=False, alias="DL_SENPAI_BILI_WRITE_ENABLE")
    bili_comment_min_gap_sec: int = Field(
        default=900, alias="DL_SENPAI_BILI_COMMENT_MIN_GAP_SEC"
    )
    bili_comment_max_chars: int = Field(default=200, alias="DL_SENPAI_BILI_COMMENT_MAX_CHARS")
    # 0 = 不按天卡条数（同小黑盒：卡条数会让她攒着补发，更像机器人）
    bili_comment_daily_limit: int = Field(
        default=0, alias="DL_SENPAI_BILI_COMMENT_DAILY_LIMIT"
    )
    bili_write_state_file: str = Field(
        default="data/dl_senpai/bili_write_state.json",
        alias="DL_SENPAI_BILI_WRITE_STATE_FILE",
    )
    # 留言轮询：B站消息接口很轻（一次 GET），但别太勤，默认半小时
    bili_inbox_enable: bool = Field(default=False, alias="DL_SENPAI_BILI_INBOX_ENABLE")
    bili_inbox_interval_sec: int = Field(
        default=1800, alias="DL_SENPAI_BILI_INBOX_INTERVAL_SEC"
    )
    bili_inbox_max_per_tick: int = Field(default=2, alias="DL_SENPAI_BILI_INBOX_MAX_PER_TICK")
    # ---- 养号（攒经验到 Lv2 才能评论）----
    # 注意：B站协议不允许自动化操作账号，这里默认关，风险自负
    bili_grow_enable: bool = Field(default=False, alias="DL_SENPAI_BILI_GROW_ENABLE")
    bili_grow_hour: int = Field(default=12, alias="DL_SENPAI_BILI_GROW_HOUR")
    # 每天投几枚硬币（0 = 不投；投币 +1 经验/枚，但那是她的硬币）
    bili_grow_coins: int = Field(default=0, alias="DL_SENPAI_BILI_GROW_COINS")
    bili_grow_log_file: str = Field(
        default="data/dl_senpai/bili_grow_log.jsonl",
        alias="DL_SENPAI_BILI_GROW_LOG_FILE",
    )
    bili_inbox_log_file: str = Field(
        default="data/dl_senpai/bili_inbox_log.jsonl",
        alias="DL_SENPAI_BILI_INBOX_LOG_FILE",
    )
    # 她自己的活动记录（刷了啥、说了啥），主人可以翻
    heybox_browse_log_file: str = Field(
        default="data/dl_senpai/heybox_browse_log.jsonl",
        alias="DL_SENPAI_HEYBOX_BROWSE_LOG_FILE",
    )
    heybox_comment_max_chars: int = Field(
        default=300, alias="DL_SENPAI_HEYBOX_COMMENT_MAX_CHARS"
    )
    # 登录态（cookie）落盘目录。里面等同于密码，别提交、别外传。
    web_login_dir: str = Field(
        default="data/dl_senpai/web_login", alias="DL_SENPAI_WEB_LOGIN_DIR"
    )
    # 登录的是主人主号就拒绝 —— 主号不许拿来发东西
    web_login_guard_owner: bool = Field(
        default=True, alias="DL_SENPAI_WEB_LOGIN_GUARD_OWNER"
    )

    def shop_auto_import_query_list(self) -> list[str]:
        raw = self.shop_auto_import_queries.strip()
        if not raw:
            return []
        return [q.strip() for q in raw.split(",") if q.strip()]

    def allowed_group_ids(self) -> set[str]:
        raw = self.allowed_groups.strip()
        if not raw:
            return set()
        return {g.strip() for g in raw.split(",") if g.strip()}

    def owner_id_set(self) -> set[str]:
        """主人 QQ 号集合。空集合 = 没有主人，走普通概率/冷却。"""
        raw = self.owner_ids.strip()
        if not raw:
            return set()
        return {q.strip() for q in raw.split(",") if q.strip()}

    def is_owner(self, user_id: object) -> bool:
        """这个人是不是主人。

        两处判定入口（新 agent 链路 / 旧编排）都走这里，避免各写一份 str()
        转换——QQ 号在不同地方有 int 和 str 两种形态，比较不统一就会出现
        「配了但不生效」这种最难查的毛病。
        """
        owners = self.owner_id_set()
        if not owners:
            return False
        return str(user_id).strip() in owners

    def own_life_interval_range(self) -> tuple[int, int]:
        """随机唤醒区间（秒）。下限保底 60 秒，上限不低于下限。

        为什么不留固定间隔：固定间隔等于闹钟，她会像打卡一样准点出现。
        随机会让她表现得像「想起来才找你」——这是原版作者自己推翻
        最初设计后给出的结论。
        """
        lo = max(60, int(self.own_life_interval_min_sec))
        hi = max(lo, int(self.own_life_interval_max_sec))
        return lo, hi

    def weekly_group_ids(self) -> list[str]:
        raw = self.weekly_groups.strip()
        if not raw:
            return []
        return [g.strip() for g in raw.split(",") if g.strip()]

    def own_life_group_ids(self) -> list[str]:
        """允许她自己发消息的群。空列表 = 只写日记，不说话。"""
        raw = self.own_life_groups.strip()
        if not raw:
            return []
        return [g.strip() for g in raw.split(",") if g.strip()]

    def checkin_group_ids(self) -> set[str]:
        raw = self.checkin_groups.strip()
        if not raw:
            return set()
        return {g.strip() for g in raw.split(",") if g.strip()}

    def is_checkin_group(self, group_id: int | str) -> bool:
        if not self.checkin_enable:
            return False
        allowed = self.checkin_group_ids()
        if not allowed:
            return False
        return str(group_id) in allowed

    def trends_source_list(self) -> list[str]:
        raw = self.trends_sources.strip()
        if not raw:
            return ["weibo", "douyin", "bilibili", "baidu", "zhihu"]
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    def trends_fallback_list(self) -> list[str]:
        raw = self.trends_fallback_bases.strip()
        if not raw:
            return []
        return [s.strip().rstrip("/") for s in raw.split(",") if s.strip()]

    def is_group_allowed(self, group_id: int | str) -> bool:
        allowed = self.allowed_group_ids()
        if not allowed:
            return True
        return str(group_id) in allowed

    def verify_group_ids(self) -> set[str]:
        raw = self.verify_groups.strip()
        if not raw:
            return set()
        return {g.strip() for g in raw.split(",") if g.strip()}

    def is_verify_group(self, group_id: int | str) -> bool:
        """是否在该群启用新人认证。"""
        if not self.verify_enable:
            return False
        if not self.is_group_allowed(group_id):
            return False
        specified = self.verify_group_ids()
        if not specified:
            return True
        return str(group_id) in specified

    def resolved_voice_tts_provider(self) -> str:
        raw = self.voice_tts_provider.strip().lower()
        if raw in {"gptsovits", "gpt-sovits", "sovits", "gsv"}:
            return "gptsovits"
        return "edge"

    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    def gptsovits_bind_host_port(self) -> tuple[str, int]:
        from urllib.parse import urlparse

        parsed = urlparse(self.gptsovits_base_url.strip())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9880
        return host, port

    def gptsovits_home_path(self) -> Path:
        raw = self.gptsovits_home.strip() or "GPT-SoVITS-v2pro-20250604"
        path = Path(raw)
        if not path.is_absolute():
            path = self.project_root() / path
        return path.resolve()

    def should_autostart_gptsovits(self) -> bool:
        # 本地 3B 推理与 SoVITS 同占 8GB 会卡死；走本地微调时不要自动拉起 TTS
        try:
            from .llm import _is_local_infer_url

            if _is_local_infer_url(self.active_base_url()):
                return False
        except Exception:  # noqa: BLE001
            pass
        return (
            self.voice_enable
            and self.gptsovits_autostart
            and self.resolved_voice_tts_provider() == "gptsovits"
        )

    def gptsovits_ref_audio_path(self) -> Path | None:
        raw = self.gptsovits_ref_audio.strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = (self.project_root() / path).resolve()
        if not path.is_file():
            return None
        return path

    def resolved_voice_settings(self) -> ResolvedVoiceSettings:
        profile = self.voice_profile.strip().lower()
        aliases = {
            "angelina": "angelina",
            "安洁莉娜": "angelina",
            "anjelina": "angelina",
            "custom": "custom",
        }
        key = aliases.get(profile, profile)
        if key in _VOICE_PROFILES:
            preset = _VOICE_PROFILES[key]
            return ResolvedVoiceSettings(
                name=preset["name"],
                rate=preset["rate"],
                pitch=preset["pitch"],
                profile=key,
            )
        return ResolvedVoiceSettings(
            name=self.voice_name.strip() or "zh-CN-XiaoyiNeural",
            rate=self.voice_rate.strip() or "+0%",
            pitch=self.voice_pitch.strip() or "+0Hz",
            profile="custom",
        )

    def provider(self) -> str:
        raw = self.llm_provider.strip().lower()
        if raw in {"ollama", "local"}:
            return "ollama"
        return "relay"

    def ollama_api_key_resolved(self) -> str:
        key = self.ollama_api_key.strip() or os.environ.get("API_Key", "").strip()
        return key or "ollama"

    def ollama_base_url_resolved(self) -> str:
        return (self.ollama_base_url.strip() or "http://127.0.0.1:11434/v1").rstrip("/")

    def ollama_model_resolved(self) -> str:
        return self.ollama_model.strip()

    def ollama_ready(self) -> bool:
        """本地 Ollama 配置是否足够作为兜底。"""
        return bool(self.ollama_model_resolved() and self.ollama_base_url_resolved())

    def active_api_key(self) -> str:
        from .speak_styles import get_speak_style_store

        style = get_speak_style_store().active()
        if style.api_key:
            return style.api_key
        if self.provider() == "ollama":
            return self.ollama_api_key_resolved()
        return self.openai_api_key.strip()

    def active_base_url(self) -> str:
        from .speak_styles import get_speak_style_store

        style = get_speak_style_store().active()
        if style.base_url:
            return style.base_url.rstrip("/")
        if self.provider() == "ollama":
            return self.ollama_base_url_resolved()
        return self.openai_base_url.strip().rstrip("/")

    def is_local_infer(self) -> bool:
        """当前风格是否指向本机微调端点（127.0.0.1 等）。"""
        from .llm import _is_local_infer_url

        return _is_local_infer_url(self.active_base_url())

    def active_model(self) -> str:
        from .speak_styles import get_speak_style_store

        style = get_speak_style_store().active()
        if style.model:
            return style.model
        if self.provider() == "ollama":
            return self.ollama_model_resolved()
        return self.openai_model.strip()

    def active_speak_style_id(self) -> str:
        from .speak_styles import get_speak_style_store

        return get_speak_style_store().active_id()

    def active_speak_style_name(self) -> str:
        from .speak_styles import get_speak_style_store

        return get_speak_style_store().active().name

    def api_configured(self) -> bool:
        if self.provider() == "ollama":
            return bool(self.active_model())
        # 风格专属 key / 全局 key 任一即可
        return bool(self.active_api_key() and self.active_model())

    def draw_api_key_resolved(self) -> str:
        key = self.draw_api_key.strip()
        if key:
            return key
        return self.active_api_key()

    def draw_base_url_resolved(self) -> str:
        url = self.draw_base_url.strip()
        if url:
            return url.rstrip("/")
        return self.active_base_url()

    def draw_model_resolved(self) -> str:
        model = self.draw_model.strip()
        return model or "gpt-image-1"

    def draw_uses_dalle_params(self) -> bool:
        return self.draw_model_resolved().lower().startswith("dall-e")

    # -- 触发 --

    def summon_keyword_list(self) -> list[str]:
        """解析召唤词；留空表示不启用召唤（退回原来的 @ / 概率插嘴行为）。

        为什么中英文逗号都认：这个值要在 `.env` 里手写，中文输入法下极容易
        打出「，」。不兼容的话表现是「静默失效」——用户只会看到学姐不理人，
        排查成本远高于这里多写一行替换。
        """
        raw = (self.summon_keywords or "").replace("，", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    # -- 知识库 --

    def knowledge_dataset_id_list(self) -> list[str]:
        """解析逗号分隔的 dataset id；留空返回空列表（表示不限定）。"""
        return [x.strip() for x in self.knowledge_dataset_ids.split(",") if x.strip()]

    def knowledge_base_url_resolved(self) -> str:
        """规范化 RAGFlow 地址（去掉尾部斜杠）。

        注意：RAGFlow 的部署前缀（如 `/ontoweb-ragflow`）是地址本身的一部分，
        这里只做尾部斜杠清理，**不会**把它当成多余的路径段去掉 ——
        少了这个前缀会直接 404（实测过）。
        """
        return self.knowledge_base_url.strip().rstrip("/")

    def knowledge_configured(self) -> bool:
        """**RAGFlow** 是否具备发起检索的最低条件（开关 + 地址 + 密钥）。

        三者缺一就不注册 provider，让 knowledge 工具保持不可见 ——
        这比注册一个每次调用都报错的 provider 更符合「默认行为明确」。

        注意：这个方法**只回答 RAGFlow 的事**。本地知识库与它无关，
        判断"有没有任何知识来源"要用 knowledge_any_configured()。
        保持这个语义是为了不破坏既有调用方与测试。
        """
        return bool(
            self.knowledge_enable
            and self.knowledge_base_url_resolved()
            and self.knowledge_api_key.strip()
        )

    def knowledge_local_dir_resolved(self) -> Path:
        """本地知识库目录的绝对路径。

        相对路径按**项目根目录**解析而不是当前工作目录 ——
        bot 可能从任何目录启动，用 cwd 会时灵时不灵。
        config.py 在 src/plugins/dl_senpai/ 下，往上三层就是项目根。
        """
        raw = str(self.knowledge_local_dir or "").strip() or "knowledge_base"
        path = Path(raw)
        if path.is_absolute():
            return path
        return Path(__file__).resolve().parents[3] / path

    def knowledge_local_configured(self) -> bool:
        """本地知识库是否可用（开关打开即算，目录不存在时按空库处理）。"""
        return bool(self.knowledge_local_enable)

    def knowledge_any_configured(self) -> bool:
        """有没有**任何**知识来源可用（RAGFlow 或本地）。

        预取与工具装配要用这个：只要本地库能用，RAGFlow 没配好
        也不该让整条链路失效。
        """
        return bool(self.knowledge_configured() or self.knowledge_local_configured())

    def draw_configured(self) -> bool:
        if not self.draw_enable:
            return False
        return bool(self.draw_api_key_resolved() and self.draw_model_resolved())


def get_config() -> PluginConfig:
    """取当前配置（**带缓存**）。

    重构前这里是 `return PluginConfig()`——每次调用都重新读一遍
    `.env` + `.env.dev` 并跑完整的 pydantic 校验。而 `handlers._handle_chat`
    一轮就要调它好几次（`_reply`、`_fe`、各 tool 内部），加上 47 个模块里的
    零散调用，等于每条 QQ 消息都在做多次磁盘 IO + 200 字段校验。

    现在委托给 `config.runtime.get_config()`：进程内缓存一个实例，
    只有 `reload_config()` 或 Provider 变更时才重建。

    保留本模块的 `get_config` 名字与签名，是为了让 47 个既有调用点
    **一行都不用改**（Preserve Behavior → Introduce Abstraction）。
    """
    from .config_runtime.runtime import get_config as _runtime_get_config

    return _runtime_get_config()
