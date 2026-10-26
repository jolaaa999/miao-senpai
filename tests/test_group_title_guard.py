# -*- coding: utf-8 -*-
"""群头衔设置的守护测试。

背景（2026-10-24 实测）：set_group_special_title 每次都被协议层拒绝
（ActionFailed: OIDB error 1013 on 0x8fc_2），随后降级去改群名片 ——
用户看到的现象是「想让头衔变，结果自己的昵称被改掉了」。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.adapters.onebot.v11.exception import ActionFailed

from dl_senpai import group_card
from dl_senpai.config import PluginConfig

GROUP = 1050332675
USER = 2843422418


def _bot(*, member_info: dict, fail_title: bool = True) -> MagicMock:
    """造一个只会失败设头衔、但能改名片的 bot。"""
    bot = MagicMock()
    bot.self_id = "2480465844"
    bot.get_group_member_info = AsyncMock(return_value=member_info)

    async def _call_api(api: str, **kwargs):
        if api == "set_group_special_title":
            if fail_title:
                raise ActionFailed(
                    status="failed",
                    retcode=100,
                    data=None,
                    wording="OIDB error 1013 on 0x8fc_2: ",
                )
            return {"status": "ok"}
        return {"status": "ok"}

    bot.call_api = _call_api
    bot.set_group_card = AsyncMock()
    bot.set_group_special_title = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_no_card_change_when_fallback_disabled(monkeypatch) -> None:
    """协议拒绝 + 兜底关闭 => 只失败，绝不动群名片。"""
    group_card._reset_title_refusal_cache()
    bot = _bot(member_info={"role": "admin"})
    monkeypatch.setattr(group_card, "_read_card", AsyncMock(return_value="原昵称"))
    result = await group_card.apply_group_title(
        bot, group_id=GROUP, user_id=USER, title="过路人", allow_card_fallback=False,
    )
    assert result.ok is False
    bot.set_group_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_change_only_when_explicitly_allowed(monkeypatch) -> None:
    """显式打开兜底时行为与旧版一致（有人确实想要这个效果）。"""
    group_card._reset_title_refusal_cache()
    bot = _bot(member_info={"role": "admin"})
    # 兜底写完会回读一次名片做验证（title card fallback verified）。
    # 所以不能一直返回旧名片：第一次读「原昵称」用来过安全护栏，
    # 之后要返回新名片，否则永远验证不过、ok 恒为 False。
    monkeypatch.setattr(
        group_card,
        "_read_card",
        AsyncMock(side_effect=["原昵称", "过路人", "过路人"]),
    )
    result = await group_card.apply_group_title(
        bot, group_id=GROUP, user_id=USER, title="过路人", allow_card_fallback=True,
        # 必须把「当前名片」声明成我们自己管理过的头衔：
        # 名片兜底有安全护栏（_card_safe_for_managed_title）——名片非空且
        # 不是我们写过的头衔就不覆盖，避免踩掉群友的真实昵称。
        # 生产三个调用点全都传了 known_managed_titles，这里补齐才与生产一致。
        known_managed_titles=frozenset({"原昵称"}),
    )
    assert result.ok is True
    assert result.method == "card"
    bot.set_group_card.assert_awaited()


@pytest.mark.asyncio
async def test_refusal_cached_to_avoid_repeat_timeout(monkeypatch) -> None:
    """被协议拒绝过就 6 小时内不再试，免得每次等 30 秒再刷一屏 traceback。"""
    group_card._reset_title_refusal_cache()
    bot = _bot(member_info={"role": "admin"})
    await group_card.apply_group_title(
        bot, group_id=GROUP, user_id=USER, title="过路人", allow_card_fallback=False,
    )
    calls_first = bot.get_group_member_info.await_count
    await group_card.apply_group_title(
        bot, group_id=GROUP, user_id=USER, title="新面孔", allow_card_fallback=False,
    )
    assert bot.get_group_member_info.await_count == calls_first, "第二次不该再打接口"


def test_config_default_is_no_card_fallback() -> None:
    """代码默认值必须是「不改名片」：头衔和名片是两回事。"""
    field = PluginConfig.model_fields["group_title_fallback_card"]
    assert field.default is False
