"""真·「正在输入」：让对方的 QQ 上真的显示「对方正在输入…」。

搬自 ai-companion-kit 的 OneBot.setTyping()。原来本地的拟人化只是
在服务端 sleep 一会儿再发——**QQ 上什么都不显示**，对方只看到消息
晚到了几秒。而这个接口能真的把状态推给对方。

但是——原项目在这一条上踩过坑，而且坑的方向很反直觉：

    我一天发了 175 次 set_input_status。真人不会每说一句都上报。

上报太勤**本身就是机器人特征**。所以他们加了三个限制：
短消息不发、最少隔 45 秒、35% 概率。175 次/天 → 约 20 次/天。

本模块的做法完全照这个来：
- 少于 min_chars 字的消息不上报（「嗯」「在」不需要装打字）
- 两次上报至少隔 min_interval_sec
- 概率抽样
- **探到不支持就永久关掉**：set_input_status 是 OneBot 扩展接口，
  NapCat 不一定实现（SnowLuma 支持）。不支持时静默降级，绝不影响发送。
"""

from __future__ import annotations

import random
import time
from typing import Any

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — 单测/独立脚本可能没有 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]

# 这些错误信息表示「这个实现根本没有这个接口」，探到一次就永久关掉。
# 1404 是 OneBot 实现的通用「不支持该 API」返回码。
_UNSUPPORTED_HINTS = ("1404", "not support", "unsupported", "not found", "unknown api")


class TypingReporter:
    """「正在输入」状态上报器。"""

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._supported: bool | None = None  # None = 还没探过
        self._last_at = 0.0
        self._sent = 0

    def _cfg(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            from .config import get_config

            return get_config()
        except Exception:  # noqa: BLE001
            return None

    def _decide(self, text: str) -> bool:
        cfg = self._cfg()
        if cfg is not None and not bool(getattr(cfg, "typing_status_enable", True)):
            return False
        if self._supported is False:
            return False

        min_chars = 20 if cfg is None else int(
            getattr(cfg, "typing_status_min_chars", 20) or 0
        )
        if len(str(text or "")) < min_chars:
            return False

        min_interval = 45.0 if cfg is None else float(
            getattr(cfg, "typing_status_min_interval_sec", 45) or 0
        )
        if min_interval > 0 and time.time() - self._last_at < min_interval:
            return False

        probability = 0.35 if cfg is None else float(
            getattr(cfg, "typing_status_probability", 0.35)
        )
        return random.random() < probability

    @staticmethod
    def _params(event: Any, on: bool) -> dict[str, Any]:
        """群聊 / 私聊的参数形状不同，跟 OneBot v11 扩展约定一致。"""
        group_id = getattr(event, "group_id", None)
        if group_id is not None:
            return {"group_id": int(group_id), "event_type": 1 if on else 0}
        return {"user_id": int(getattr(event, "user_id")), "event_type": 1 if on else 0}

    async def _call(self, bot: Any, event: Any, on: bool) -> bool:
        try:
            await bot.call_api("set_input_status", **self._params(event, on))
        except Exception as exc:  # noqa: BLE001 — 上报失败绝不能影响发消息
            message = str(exc).lower()
            if self._supported is None and any(h in message for h in _UNSUPPORTED_HINTS):
                # 首次探测就明确不支持：永久关掉，以后连试都不试
                self._supported = False
                logger.info(
                    f"dl_senpai typing status unsupported by this OneBot impl, disabled: {exc}"
                )
            else:
                logger.warning(f"dl_senpai set_input_status failed: {exc}")
            return False
        self._supported = True
        return True

    async def start(self, bot: Any, event: Any, text: str) -> bool:
        """开始「正在输入」。返回是否真的上报了（调用方据此决定要不要 stop）。"""
        if not self._decide(text):
            return False
        self._last_at = time.time()
        ok = await self._call(bot, event, True)
        if ok:
            self._sent += 1
        return ok

    async def stop(self, bot: Any, event: Any) -> None:
        await self._call(bot, event, False)

    def status(self) -> dict[str, Any]:
        return {
            "supported": self._supported,
            "sent": self._sent,
            "last_at": self._last_at,
        }


_reporter: TypingReporter | None = None


def get_typing_reporter(config: Any = None) -> TypingReporter:
    global _reporter
    if config is not None:
        return TypingReporter(config)
    if _reporter is None:
        _reporter = TypingReporter()
    return _reporter


def reset_typing_reporter() -> None:
    """测试用：清掉单例（含「已探测为不支持」的缓存）。"""
    global _reporter
    _reporter = None
