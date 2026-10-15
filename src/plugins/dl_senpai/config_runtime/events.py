"""配置变更事件：`config.changed`。

本模块只负责"把变更通知出去"，不关心谁在听。Phase 2 先把机制建起来，
Phase 3 再让需要的模块订阅（例如 LLM 客户端在 key 变化时重建连接）。

与 `core/event_bus` 的关系：这里是**轻量回调**，不依赖 EventBus 实例存在。
原因：配置是几乎所有模块的依赖，如果 config 反过来依赖 core.event_bus，
会形成 `core → config → core` 的循环。因此这里定义独立的回调协议，
由 runtime 持有；需要 EventBus 语义时，可在 Phase 3 注册一个"把回调
转成事件"的桥接订阅者。
"""

from __future__ import annotations

from typing import Any, Callable

# 回调签名：(old_version, new_version) -> None
ConfigChangedCallback = Callable[[int, int], Any]


def notify_config_changed(
    subscribers: list[ConfigChangedCallback],
    old_version: int,
    new_version: int,
) -> int:
    """依次通知订阅者；返回成功通知数量。

    异常隔离：某个订阅者抛异常不影响其他订阅者，也不影响触发方
    （reload 必须永远成功，否则配置会卡在坏状态）。
    """
    if old_version == new_version:
        return 0
    delivered = 0
    for callback in list(subscribers):
        try:
            callback(old_version, new_version)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            try:
                from ..observability.logging import log_event

                log_event(
                    "config.changed_handler_failed",
                    level="warning",
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001
                pass
    try:
        from ..observability.metrics import metrics

        metrics.incr("config.changed")
    except Exception:  # noqa: BLE001
        pass
    return delivered


def publish_to_bus(bus: Any, old_version: int, new_version: int) -> int:
    """把配置变更桥接成 EventBus 上的 `config.changed` 事件。

    Phase 3 接线用：`runtime.on_changed(lambda o, n: publish_to_bus(bus, o, n))`
    """
    if bus is None:
        return 0
    try:
        from ..core.event_bus import publish_config_changed

        return publish_config_changed(bus, old_version, new_version)
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["ConfigChangedCallback", "notify_config_changed", "publish_to_bus"]
