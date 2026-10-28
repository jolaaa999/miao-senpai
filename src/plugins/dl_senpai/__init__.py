"""深度学习学姐插件：热情可爱、@必回、低频率插嘴。"""

from __future__ import annotations

import asyncio

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot
from nonebot.plugin import PluginMetadata

from .config import get_config
from .observability.logging import log_event

__plugin_meta__ = PluginMetadata(
    name="dl_senpai",
    description="深度学习学姐：OpenAI 兼容中转 + 群聊记忆 + 跨群长时印象 + 插嘴 + 识图 + 生图 + 表情包 + 语音 + 热梗 + 联网 + 签到 + 积分服装店 + 新人认证",
    usage=(
        "@学姐 提问；未 @ 时低概率自然插嘴；自动收集并复用群聊表情包；"
        "可发语音条、文生图；新人进群自动欢迎；限时 @学姐「认证」否则踢出；"
        "群管时可改群名片/生气禁言；可接近期热梗与实时联网检索；"
        "按 QQ 跨群记住每个人的特征；指定群可签到/积分/称号/任务"
    ),
    type="application",
    homepage="",
    config=None,
    supported_adapters={"~onebot.v11"},
)

# 延迟导入 handlers，避免仅测子模块时强依赖驱动器
from . import handlers as _handlers  # noqa: E402, F401
from . import social_accept as _social_accept  # noqa: E402, F401

try:
    driver = get_driver()
except ValueError:
    driver = None


if driver is not None:

    @driver.on_startup
    async def _on_startup() -> None:
        cfg = get_config()
        # 配置热重载：Web 后台改 .env 后无需重启即可生效
        from .config_watcher import get_watcher

        get_watcher().start()
        status = "已配置" if cfg.api_configured() else "未配置（仅提示）"
        logger.info(
            f"[dl_senpai] provider={cfg.provider()} model={cfg.active_model()} "
            f"style={cfg.active_speak_style_id()}/{cfg.active_speak_style_name()} "
            f"base={cfg.active_base_url()} key={status} "
            f"interrupt_prob={cfg.interrupt_prob} "
            f"stickers={'on' if cfg.sticker_enable else 'off'} "
            f"card={'on' if cfg.card_enable else 'off'} "
            f"mute={'on' if cfg.mute_enable else 'off'} "
            f"voice={'on' if cfg.voice_enable else 'off'} "
            f"voice_tts={cfg.resolved_voice_tts_provider()} "
            f"voice_profile={cfg.resolved_voice_settings().profile} "
            f"trends={'on' if cfg.trends_enable else 'off'} "
            f"search={'on' if cfg.search_enable else 'off'} "
            f"draw={'on' if cfg.draw_enable else 'off'} "
            f"browser={'on' if cfg.browser_enable else 'off'} "
            f"accept_friend={'on' if cfg.auto_accept_friend else 'off'} "
            f"accept_invite={'on' if cfg.auto_accept_group else 'off'} "
            f"person_memory={'on' if cfg.person_memory_enable else 'off'} "
            f"verify={'on' if cfg.verify_enable else 'off'} "
            f"checkin={'on' if cfg.checkin_enable else 'off'} "
            # 干跑是最容易忘关的开关，放最前面且写明「不会真发」
            f"dry_run={'ON!不会真发消息' if cfg.dry_run else 'off'} "
            f"breaker={'on' if cfg.send_breaker_enable else 'off'}"
            f"({cfg.send_rate_per_minute}/min,{cfg.send_rate_per_hour}/h) "
            f"typing_status={'on' if cfg.typing_status_enable else 'off'} "
            # 在场状态是这一版「活人味」的骨架：等下文多久、说完先听多久、
            # 潜水区间。出问题第一个要看这几个数，所以直接打出来。
            f"presence={'on' if cfg.presence_enable else 'off'}"
            f"(wait={cfg.pending_wait_ms}ms observe={cfg.presence_observe_ms}ms "
            f"dive={cfg.presence_dive_min_ms}~{cfg.presence_dive_max_ms}ms "
            f"pre_sleep={cfg.presence_pre_sleep_ms}ms) "
            f"diary={'on' if cfg.diary_enable else 'off'} "
            f"backfill={'on' if cfg.backfill_enable else 'off'} "
            f"asr={'on' if cfg.asr_enable else 'off'} "
            f"gallery={'on' if cfg.gallery_enable else 'off'} "
            f"weekly={'on' if cfg.weekly_enable else 'off'} "
            f"computer={'on:' + cfg.computer_mode if cfg.computer_enable else 'off'} "
            f"link_read={'on' if cfg.link_read_enable else 'off'} "
            # B站/小黑盒各自一个开关，横幅里必须能一眼看到（出问题先看这行）
            f"bili={'on' if cfg.bili_enable else 'off'} "
            f"heybox={'on' if cfg.heybox_enable else 'off'} "
            f"bili_browse={'on' if cfg.bili_browse_enable else 'off'}"
            f"(every={cfg.bili_browse_interval_min_sec}~{cfg.bili_browse_interval_max_sec}s "
            f"max={cfg.bili_browse_daily_max}) "
            f"bili_grow={'on' if cfg.bili_grow_enable else 'off'}"
            f"(hour={cfg.bili_grow_hour}) "
            f"bili_inbox={'on' if cfg.bili_inbox_enable else 'off'}"
            f"(every={cfg.bili_inbox_interval_sec}s) "
            f"browse={'on' if cfg.heybox_browse_enable else 'off'}"
            f"(every={cfg.heybox_browse_interval_min_sec}~{cfg.heybox_browse_interval_max_sec}s "
            f"p={cfg.heybox_browse_comment_probability}) "
            f"own_life={'on' if cfg.own_life_enable else 'off'}"
            f"(interval={cfg.own_life_interval_sec}s cap={cfg.own_life_max_per_day}/day "
            f"groups={len(cfg.own_life_group_ids())})"
        )
        if cfg.trends_enable:
            asyncio.create_task(_warmup_trends())

        # 技能层（默认关）：启动时打一行清单，让"到底装了哪些技能、触发词是什么"
        # 在日志里可见。故意只记日志、不注入提示：技能靠命中触发词才会进上下文，
        # 把清单常驻进提示是纯开销（见 skills.catalog_text 的说明）。
        if cfg.skill_enable:
            try:
                from .skills import catalog_text, get_skill_registry

                registry = get_skill_registry(cfg.skill_dir)
                specs = registry.all()
                log_event(
                    "skills.ready",
                    count=len(specs),
                    names=",".join(s.name for s in specs) or None,
                    catalog=catalog_text(registry) or None,
                )
            except Exception as exc:  # noqa: BLE001 — 技能目录坏了不能挡住上线
                log_event("skills.load_failed", level="warning", error=f"{type(exc).__name__}: {exc}")

        # MCP 外部工具（默认关）。放到后台任务里拉起：它要连第三方进程或外部地址，
        # 连不上最多是少几个工具，绝不能把机器人上线挡住。
        if cfg.mcp_enable:
            asyncio.create_task(_start_mcp())

        if cfg.should_autostart_gptsovits():
            from .gptsovits_server import start_gptsovits_server

            await start_gptsovits_server(cfg)

    @driver.on_bot_connect
    async def _on_bot_connect(bot: Bot) -> None:
        from .group_verify import reschedule_pending_kicks
        from .group_features import register_groups

        try:
            await reschedule_pending_kicks(bot)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dl_senpai] verify reschedule skipped: {e}")
        asyncio.create_task(register_groups(bot))
        # 商店素材后台预热：货架每天 4 点换新，缩略图单张约 85 秒，
        # 不预热的话用户早上第一句「商店」要等 4 分半（实测 4 分 19 秒）。
        from .shop_warmup import shop_warmup_loop

        asyncio.create_task(shop_warmup_loop())

        # 她自己的生活：没人找她的时候也会自己醒一次（默认关闭，见配置）。
        # 放在 bot_connect 而不是 startup：它要往外发消息，必须有活的 bot。
        from .own_life import own_life_loop

        asyncio.create_task(own_life_loop(bot))
        # 定时消息（喵/定时）：这轮循环只负责「到点发出去」，
        # 任务本身在 data/dl_senpai/admin/schedule.json 里，重启不丢。
        from .admin_schedule import timer_loop  # noqa: E402 — 与上面几个循环同一批延迟导入

        asyncio.create_task(timer_loop(bot), name="dl_senpai-timer-loop")

        # 她自己刷帖子：隔一阵去小黑盒首页逛逛，有话说就评一句（默认关闭）。
        # 不放在 startup：它要用登录态开浏览器，跟 bot 是否在线无关，
        # 但跟"同一时间只开一个浏览器"有关，所以跟聊天共用一个浏览器锁。
        from .heybox_browse import browse_loop

        asyncio.create_task(browse_loop())

        # B站留言：官方消息接口，一次 GET 就能看有没有人回（默认关闭）。
        from .bili_inbox import bili_inbox_loop

        asyncio.create_task(bili_inbox_loop())

        # B站养号：每天攒一点经验到 Lv2（那是 B 站允许评论的门槛）。默认关。
        from .bili_grow import bili_grow_loop

        asyncio.create_task(bili_grow_loop())

        # 自主刷B站：看视频、读字幕、点赞、偶尔收藏，写日记/记忆本。默认关闭。
        from .bili_browse import bili_browse_loop

        asyncio.create_task(bili_browse_loop())

        # 自主刷抖音：走浏览器拦响应（必须登录，慢），只读描述+热评。默认关闭。
        from .douyin_browse import browse_loop as douyin_browse_loop

        asyncio.create_task(douyin_browse_loop())

        # 断线补课：连接是刚刚才建立起来的，所以这里是**唯一**能补的时机。
        # 补课结果不会直接发消息，只写一份摘要等下一轮拼提示词时注入一次。
        from .backfill import run_backfill

        try:
            patch = await run_backfill(bot, get_config())
            if patch not in ("disabled", "empty"):
                logger.info(f"[dl_senpai] backfill {patch}")
        except Exception as e:  # noqa: BLE001 — 补课失败绝不能影响聊天
            logger.warning(f"[dl_senpai] backfill skipped: {e}")

        # 周报：每半小时看一眼是不是到点了（默认关闭）。
        from .weekly import weekly_loop

        asyncio.create_task(weekly_loop(bot))


    @driver.on_shutdown
    async def _on_shutdown() -> None:
        from .config_watcher import get_watcher
        from .gptsovits_server import stop_gptsovits_server

        await get_watcher().stop()
        await stop_gptsovits_server()
        # MCP：摘掉注册的工具并断开所有服务器连接（未开启时是空操作）
        await _stop_mcp()


    async def _start_mcp() -> None:
        """后台拉起 MCP 工具接入，并把结果写进日志。

        单独一行日志是刻意的：启动横幅是几十个字段的长行，
        「MCP 到底连上了没」这种要立刻回答的问题不该再去长行里找。
        """
        from .mcp_setup import start_mcp_tools

        status = await start_mcp_tools()
        logger.info(
            f"[dl_senpai] mcp state={status.get('state')} "
            f"servers={status.get('servers')} tools={status.get('tools')} "
            f"errors={status.get('errors') or {}}"
        )

    async def _stop_mcp() -> None:
        from .mcp_setup import stop_mcp_tools

        await stop_mcp_tools()

    async def _warmup_trends() -> None:
        try:
            from .trends import get_trends_store

            store = get_trends_store()
            snap = await store.get_snapshot(force=True)
            n = len(snap.items) if snap else 0
            logger.info(f"[dl_senpai] trends warmup items={n}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dl_senpai] trends warmup skipped: {e}")
