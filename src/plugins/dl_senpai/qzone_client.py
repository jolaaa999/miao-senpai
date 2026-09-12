"""onebot-qzone（A 方案）HTTP 客户端：好友动态点赞 / 评论。

独立桥接服务默认 http://127.0.0.1:5700；可选用 NapCat get_cookies
同步到桥接的 login_cookie，免手拷 Cookie。
"""

from __future__ import annotations

from typing import Any

import httpx

try:
    from nonebot import logger
except ImportError:
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]


class QZoneBridgeError(RuntimeError):
    pass


class QZoneBridgeClient:
    def __init__(
        self,
        base_url: str,
        *,
        access_token: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.access_token = (access_token or "").strip()
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        if not self.configured():
            raise QZoneBridgeError("qzone bridge url empty")
        # onebot-qzone HTTP：action 在路径上 POST /{action}，body 即 params
        # （POST / + {"action":...} 会得到空路径 → retcode 1404「不支持的 action:」）
        url = f"{self.base_url}/{action.lstrip('/')}"
        payload = params or {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
            except Exception as exc:  # noqa: BLE001
                raise QZoneBridgeError(f"bridge unreachable: {exc}") from exc
            if resp.status_code == 404:
                # 兼容少数实现：根路径 OneBot 信封
                resp = await client.post(
                    f"{self.base_url}/",
                    json={"action": action, "params": payload},
                    headers=self._headers(),
                )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise QZoneBridgeError(
                f"bad json status={resp.status_code} body={resp.text[:200]}"
            ) from exc
        if not isinstance(data, dict):
            raise QZoneBridgeError(f"unexpected response: {data!r}")
        status = str(data.get("status") or "").lower()
        retcode = data.get("retcode", data.get("ret"))
        if status and status != "ok":
            raise QZoneBridgeError(
                f"{action} failed status={status} retcode={retcode} "
                f"msg={data.get('message') or data.get('wording')}"
            )
        if retcode not in (None, 0, "0"):
            raise QZoneBridgeError(
                f"{action} failed retcode={retcode} msg={data.get('message')}"
            )
        return data.get("data", data)

    async def login_cookie(self, cookie: str) -> Any:
        return await self.call("login_cookie", {"cookie": cookie})

    async def get_friend_feeds(
        self, *, num: int = 20, cursor: str = "", include_image_data: bool = False
    ) -> Any:
        params: dict[str, Any] = {
            "num": num,
            "count": num,
            "include_image_data": include_image_data,
            "fast_mode": 1,
        }
        if cursor:
            params["cursor"] = cursor
        return await self.call("get_friend_feeds", params)

    async def like_feed(self, user_id: int | str, tid: str, abstime: str | int = "") -> Any:
        params: dict[str, Any] = {"user_id": int(user_id), "tid": str(tid)}
        if abstime not in ("", None):
            params["abstime"] = abstime
        return await self.call("send_like", params)

    async def comment_feed(
        self, user_id: int | str, tid: str, content: str
    ) -> Any:
        return await self.call(
            "send_comment",
            {
                "target_uin": int(user_id),
                "target_tid": str(tid),
                "content": content,
            },
        )


def normalize_feeds(payload: Any) -> list[dict[str, Any]]:
    """把桥接返回整理成 [{user_id, tid, abstime, nickname, summary}, ...]。"""
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("list", "feeds", "data", "items", "msglist"):
            v = payload.get(key)
            if isinstance(v, list):
                items = v
                break
        else:
            items = []
    else:
        items = []

    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        uid = (
            row.get("user_id")
            or row.get("uin")
            or row.get("owner_uin")
            or row.get("uid")
            or user.get("uin")
        )
        tid = row.get("tid") or row.get("cellid") or row.get("id") or row.get("fid")
        if uid is None or tid is None:
            continue
        abstime = row.get("abstime") or row.get("createTime") or row.get("created_time") or ""
        nick = row.get("nickname") or row.get("name") or user.get("name") or ""
        summary = str(
            row.get("content")
            or row.get("summary")
            or row.get("rt_con")
            or row.get("text")
            or ""
        )[:120]
        liked = bool(row.get("isLiked") or row.get("islike") or row.get("liked"))
        out.append(
            {
                "user_id": str(uid),
                "tid": str(tid),
                "abstime": abstime,
                "nickname": str(nick or ""),
                "summary": summary,
                "liked": liked,
            }
        )
    return out
