from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    """学姐插件配置（环境变量前缀 DL_SENPAI_ / OPENAI_ / OLLAMA_ / LLM_）。"""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.dev"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

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

    interrupt_prob: float = Field(default=0.10, alias="DL_SENPAI_INTERRUPT_PROB")
    interrupt_cooldown: int = Field(default=90, alias="DL_SENPAI_INTERRUPT_COOLDOWN")
    # 刚 @ 学姐后，多少秒内再发表情包也算找她（QQ 不便同条 @+表情）
    sticker_followup_sec: int = Field(default=60, alias="DL_SENPAI_STICKER_FOLLOWUP_SEC")
    min_msg_len: int = Field(default=8, alias="DL_SENPAI_MIN_MSG_LEN")
    max_history: int = Field(default=0, alias="DL_SENPAI_MAX_HISTORY")
    temperature: float = Field(default=1.05, alias="DL_SENPAI_TEMPERATURE")
    max_tokens: int = Field(default=1600, alias="DL_SENPAI_MAX_TOKENS")
    interrupt_max_tokens: int = Field(default=200, alias="DL_SENPAI_INTERRUPT_MAX_TOKENS")
    reply_max_chars: int = Field(default=1500, alias="DL_SENPAI_REPLY_MAX_CHARS")
    reply_chunk_chars: int = Field(default=900, alias="DL_SENPAI_REPLY_CHUNK_CHARS")
    reply_hard_max_chars: int = Field(default=8000, alias="DL_SENPAI_REPLY_HARD_MAX_CHARS")
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

    # 拟人化：口语 few-shot / 碎句发送 / 内心状态 / 群风格 / 记忆召回
    fewshot_enable: bool = Field(default=True, alias="DL_SENPAI_FEWSHOT_ENABLE")
    humanize_send: bool = Field(default=True, alias="DL_SENPAI_HUMANIZE_SEND")
    typing_delay_ms_min: int = Field(default=400, alias="DL_SENPAI_TYPING_DELAY_MS_MIN")
    typing_delay_ms_max: int = Field(default=1200, alias="DL_SENPAI_TYPING_DELAY_MS_MAX")
    bubble_max: int = Field(default=3, alias="DL_SENPAI_BUBBLE_MAX")
    bubble_min_chars: int = Field(default=40, alias="DL_SENPAI_BUBBLE_MIN_CHARS")
    bubble_prefer_chars: int = Field(default=120, alias="DL_SENPAI_BUBBLE_PREFER_CHARS")

    inner_state_enable: bool = Field(default=True, alias="DL_SENPAI_INNER_STATE_ENABLE")
    inner_state_dir: str = Field(
        default="data/dl_senpai/inner_state", alias="DL_SENPAI_INNER_STATE_DIR"
    )
    inner_state_per_group: bool = Field(
        default=True, alias="DL_SENPAI_INNER_STATE_PER_GROUP"
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
    memory_recall_top: int = Field(default=4, alias="DL_SENPAI_MEMORY_RECALL_TOP")

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
    search_enable: bool = Field(default=True, alias="DL_SENPAI_SEARCH_ENABLE")
    search_auto: bool = Field(
        default=True,
        alias="DL_SENPAI_SEARCH_AUTO",
        description="时效/事实类问题在调用模型前自动联网，无需 <<<SEARCH>>>",
    )
    search_auto_aggressive: bool = Field(
        default=True,
        alias="DL_SENPAI_SEARCH_AUTO_AGGRESSIVE",
        description="对像问事实/背景的消息更积极自动联网（类似网页版助手）",
    )
    search_provider: str = Field(default="auto", alias="DL_SENPAI_SEARCH_PROVIDER")
    search_max_results: int = Field(default=5, alias="DL_SENPAI_SEARCH_MAX_RESULTS")
    search_max_rounds: int = Field(default=3, alias="DL_SENPAI_SEARCH_MAX_ROUNDS")
    search_max_auto_queries: int = Field(default=4, alias="DL_SENPAI_SEARCH_MAX_AUTO_QUERIES")
    search_timeout: float = Field(default=12.0, alias="DL_SENPAI_SEARCH_TIMEOUT")
    search_region: str = Field(default="zh-cn", alias="DL_SENPAI_SEARCH_REGION")
    search_prompt_max_chars: int = Field(
        default=2400, alias="DL_SENPAI_SEARCH_PROMPT_MAX_CHARS"
    )
    search_allow_on_interrupt: bool = Field(
        default=False, alias="DL_SENPAI_SEARCH_ALLOW_ON_INTERRUPT"
    )
    search_searxng_url: str = Field(default="", alias="DL_SENPAI_SEARCH_SEARXNG_URL")
    search_tavily_api_key: str = Field(default="", alias="DL_SENPAI_SEARCH_TAVILY_API_KEY")

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

    # 主动社交：遍历群成员发好友申请（默认关，防风控）
    # mode: web=Cookie CGI（NapCat 推荐）；auto=CGI 失败再探测 OneBot 一次；onebot=仅协议
    friend_add_enable: bool = Field(default=False, alias="DL_SENPAI_FRIEND_ADD_ENABLE")
    friend_add_mode: str = Field(default="web", alias="DL_SENPAI_FRIEND_ADD_MODE")
    friend_add_daily_limit: int = Field(default=20, alias="DL_SENPAI_FRIEND_ADD_DAILY_LIMIT")
    friend_add_verify_msg: str = Field(
        default="我是群里的学姐~", alias="DL_SENPAI_FRIEND_ADD_VERIFY_MSG"
    )
    friend_add_exclude: str = Field(
        default="", alias="DL_SENPAI_FRIEND_ADD_EXCLUDE",
        description="逗号分隔的 QQ 号，永不发好友申请",
    )
    owner_ids: str = Field(
        default="",
        alias="DL_SENPAI_OWNER_IDS",
        description="逗号分隔主人 QQ；可私聊「导出未加好友」（另认 SUPERUSERS）",
    )

    # 本机 QQ 客户端 RPA 加好友（独立进程点 UI，需校准坐标）
    friend_rpa_enable: bool = Field(default=True, alias="DL_SENPAI_FRIEND_RPA_ENABLE")
    friend_rpa_mode: str = Field(default="deeplink", alias="DL_SENPAI_FRIEND_RPA_MODE")
    friend_rpa_verify_msg: str = Field(
        default="我是群里的学姐~", alias="DL_SENPAI_FRIEND_RPA_VERIFY_MSG"
    )
    friend_rpa_delay_min: float = Field(default=8.0, alias="DL_SENPAI_FRIEND_RPA_DELAY_MIN")
    friend_rpa_delay_max: float = Field(default=18.0, alias="DL_SENPAI_FRIEND_RPA_DELAY_MAX")
    friend_rpa_max_per_run: int = Field(
        default=20, alias="DL_SENPAI_FRIEND_RPA_MAX_PER_RUN"
    )

    # 主动社交：每日给好友主页点赞（send_like，每人最多 10 赞/天；默认关）
    like_enable: bool = Field(default=False, alias="DL_SENPAI_LIKE_ENABLE")
    like_daily_hour: int = Field(default=10, alias="DL_SENPAI_LIKE_DAILY_HOUR")
    like_times: int = Field(default=10, alias="DL_SENPAI_LIKE_TIMES")

    # QQ 空间动态：对接 onebot-qzone HTTP 桥（A 方案）
    qzone_enable: bool = Field(default=False, alias="DL_SENPAI_QZONE_ENABLE")
    qzone_autostart: bool = Field(
        default=True,
        alias="DL_SENPAI_QZONE_AUTOSTART",
        description="bot.py 启动时是否自动拉起 onebot-qzone 子进程",
    )
    qzone_dir: str = Field(
        default="onebot-qzone",
        alias="DL_SENPAI_QZONE_DIR",
        description="onebot-qzone 目录（相对项目根或绝对路径）",
    )
    qzone_startup_timeout: float = Field(
        default=60.0, alias="DL_SENPAI_QZONE_STARTUP_TIMEOUT"
    )
    qzone_bridge_url: str = Field(
        default="http://127.0.0.1:5700", alias="DL_SENPAI_QZONE_BRIDGE_URL"
    )
    qzone_access_token: str = Field(default="", alias="DL_SENPAI_QZONE_ACCESS_TOKEN")
    qzone_timeout: float = Field(default=30.0, alias="DL_SENPAI_QZONE_TIMEOUT")
    qzone_sync_cookie: bool = Field(
        default=True,
        alias="DL_SENPAI_QZONE_SYNC_COOKIE",
        description="用 NapCat get_cookies 同步到桥接 login_cookie",
    )
    qzone_daily_hour: int = Field(default=10, alias="DL_SENPAI_QZONE_DAILY_HOUR")
    qzone_like_enable: bool = Field(default=True, alias="DL_SENPAI_QZONE_LIKE_ENABLE")
    qzone_comment_enable: bool = Field(
        default=True, alias="DL_SENPAI_QZONE_COMMENT_ENABLE"
    )
    qzone_like_daily_limit: int = Field(
        default=30, alias="DL_SENPAI_QZONE_LIKE_DAILY_LIMIT"
    )
    qzone_comment_daily_limit: int = Field(
        default=10, alias="DL_SENPAI_QZONE_COMMENT_DAILY_LIMIT"
    )
    qzone_comment_prob: float = Field(
        default=0.35, alias="DL_SENPAI_QZONE_COMMENT_PROB"
    )
    qzone_comment_templates: str = Field(
        default="好看！,冲！,学姐路过点个赞~,不错哦",
        alias="DL_SENPAI_QZONE_COMMENT_TEMPLATES",
        description="LLM 失败时的兜底池；正常情况按动态内容生成",
    )
    qzone_comment_llm: bool = Field(
        default=True,
        alias="DL_SENPAI_QZONE_COMMENT_LLM",
        description="空间评论是否用模型按动态内容生成",
    )
    qzone_feed_num: int = Field(default=20, alias="DL_SENPAI_QZONE_FEED_NUM")

    # 签到：默认仅指定群启用
    checkin_enable: bool = Field(default=True, alias="DL_SENPAI_CHECKIN_ENABLE")
    checkin_groups: str = Field(
        default="980229149,600478436,1050332675", alias="DL_SENPAI_CHECKIN_GROUPS"
    )
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

    def qzone_home_path(self) -> Path:
        raw = (self.qzone_dir or "").strip() or "onebot-qzone"
        path = Path(raw)
        if not path.is_absolute():
            path = self.project_root() / path
        return path.resolve()

    def qzone_bind_host_port(self) -> tuple[str, int]:
        from urllib.parse import urlparse

        parsed = urlparse(self.qzone_bridge_url.strip() or "http://127.0.0.1:5700")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 5700
        return host, port

    def should_autostart_qzone(self) -> bool:
        return bool(self.qzone_enable and self.qzone_autostart)

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

    def draw_configured(self) -> bool:
        if not self.draw_enable:
            return False
        return bool(self.draw_api_key_resolved() and self.draw_model_resolved())


def get_config() -> PluginConfig:
    return PluginConfig()
