"""深度学习学姐插件：热情可爱、@必回、低频率插嘴。"""

from __future__ import annotations

import asyncio

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot
from nonebot.plugin import PluginMetadata

from .config import get_config

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
from . import friend_candidates as _friend_candidates  # noqa: E402, F401

try:
    driver = get_driver()
except ValueError:
    driver = None


if driver is not None:

    @driver.on_startup
    async def _on_startup() -> None:
        cfg = get_config()
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
            f"friend_add={'on' if cfg.friend_add_enable else 'off'} "
            f"like={'on' if cfg.like_enable else 'off'} "
            f"qzone={'on' if cfg.qzone_enable else 'off'} "
            f"person_memory={'on' if cfg.person_memory_enable else 'off'} "
            f"verify={'on' if cfg.verify_enable else 'off'} "
            f"checkin={'on' if cfg.checkin_enable else 'off'}"
        )
        if cfg.trends_enable:
            asyncio.create_task(_warmup_trends())

        if cfg.should_autostart_gptsovits():
            from .gptsovits_server import start_gptsovits_server

            await start_gptsovits_server(cfg)

        if cfg.should_autostart_qzone():
            from .qzone_server import start_qzone_bridge

            await start_qzone_bridge(cfg)

    @driver.on_bot_connect
    async def _on_bot_connect(bot: Bot) -> None:
        from .group_verify import reschedule_pending_kicks
        from .group_features import register_groups

        try:
            await reschedule_pending_kicks(bot)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dl_senpai] verify reschedule skipped: {e}")
        asyncio.create_task(register_groups(bot))

        from .config import get_config as _cfg
        from .social_proactive import social_loop

        # 不传入 config 快照：循环内每轮 get_config()，.env 开关改完重启即生效
        asyncio.create_task(social_loop(bot))

        # NapCat 连上后：用 Cookie 登录空间桥（桥进程已由 startup 拉起）
        cfg = _cfg()
        if cfg.qzone_enable and cfg.qzone_sync_cookie:
            async def _sync_qzone() -> None:
                # 等桥端口就绪再推 Cookie（autostart 竞态）
                try:
                    from .qzone_server import is_port_open, wait_for_bridge_ready
                    from .qzone_sync import sync_napcat_cookies_to_qzone

                    host, port = cfg.qzone_bind_host_port()
                    if not is_port_open(host, port):
                        await wait_for_bridge_ready(host, port, min(30.0, cfg.qzone_startup_timeout))
                    await asyncio.sleep(1.0)
                    info = await sync_napcat_cookies_to_qzone(bot, cfg)
                    logger.info(
                        f"[dl_senpai] qzone login via NapCat ok "
                        f"uin={info.get('uin')} bridge_hot={info.get('bridge_hot')}"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[dl_senpai] qzone napcat cookie sync skipped: {e}")

            asyncio.create_task(_sync_qzone())

    @driver.on_shutdown
    async def _on_shutdown() -> None:
        from .gptsovits_server import stop_gptsovits_server
        from .qzone_server import stop_qzone_bridge

        await stop_gptsovits_server()
        await stop_qzone_bridge()


    async def _warmup_trends() -> None:
        try:
            from .trends import get_trends_store

            store = get_trends_store()
            snap = await store.get_snapshot(force=True)
            n = len(snap.items) if snap else 0
            logger.info(f"[dl_senpai] trends warmup items={n}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dl_senpai] trends warmup skipped: {e}")
