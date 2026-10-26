"""写入层的两个工具：发说说（QZONE）和黑盒评论（HEYPOST）。

为什么这两个单独一个文件、单独一批开关
----------------------------------------
它们跟只读层不是一个量级：只读最坏是被站点限流，这两个是**以她的名义
在公开平台留痕**，发出去撤不回来。所以：

1. 默认关（qzone_enable / heybox_write_enable）。
2. 工具名进 permissions.HIGH_RISK_TOOLS —— 非管理员连名字都看不到。
3. 执行时再验一次触发者：不是主人本人，直接拒。
4. 说说默认只存草稿；要真发，得主人**在私聊里**说一句（qzone_auto_post）。

四层是串联的，少一层都不行。
"""

from __future__ import annotations

import re
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

__all__ = [
    "build_write_tools",
    "execute_bili_write",
    "execute_heybox_write",
    "execute_qzone",
]

QZONE_MARKER = "QZONE"
HEYPOST_MARKER = "HEYPOST"
BILIPOST_MARKER = "BILIPOST"

QZONE_PROMPT_TEXT = """你可以写一条 QQ 空间说说，但发不发由主人说了算。

用法：
    <<<QZONE draft 今天晚饭煮了番茄面，真香>>>
    <<<QZONE list>>>
    <<<QZONE publish>>>
    <<<QZONE publish 1714041234-2>>>
    <<<QZONE drop 1714041234-2>>>
    <<<QZONE drop all>>>

规则：
- draft 只是存草稿，**不会发出去**，写完告诉主人「记下了，要发吗」
- publish 只有主人说「发」才能用；群里别人说「发」不算，直接拒
- 内容像人话：一到两句，不抒情、不升华、不放表情轰炸
- 存完可以用 list 复核；写坏了就 drop 掉（只删没发的，发过的不删）
- 不写链接、不写手机号、不做加群引流（会被判广告）
- 不要向她人展示这个 marker，也不要说「我正在调用接口」"""

HEYPOST_PROMPT_TEXT = """你可以在小黑盒某个帖子下面回一条评论（只读层搜到的帖子都能评）。

用法：
    <<<HEYPOST 8e7f729adcb3 | 这套配装我试了，斩杀确实稳>>>

规则：
- 只有主人明确让你去回，你才用；别人让你去回，一律说「这个得主人点头」
- 先 HEYBOX read 读过帖子再评，别凭标题评
- 三百字以内，一句到三句，别写成小作文
- 不带链接、不带手机号
- 不要暴露 marker；不说「正在提交评论」这种机械话"""

_QZONE_RE = re.compile(
    r"<<<QZONE\s+(draft|list|publish|drop)\s*([^>\n]*?)\s*(?:>>>|＞＞＞)", re.IGNORECASE
)
_QZONE_TAIL_RE = re.compile(r"<<<QZONE\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
# B站：两段式发顶层评论 <<<BILIPOST BV1xx411c7mD|内容>>>，
# 四段式回某条留言 <<<BILIPOST reply BV1xx411c7mD|root_id|parent_id|内容>>>
_BILIPOST_REPLY_RE = re.compile(
    r"<<<BILIPOST\s+reply\s+([0-9A-Za-z]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^>\n]+?)\s*(?:>>>|＞＞＞)",
    re.IGNORECASE,
)
_BILIPOST_RE = re.compile(
    r"<<<BILIPOST\s+([0-9A-Za-z]+)\s*\|\s*([^>\n]+?)\s*(?:>>>|＞＞＞)", re.IGNORECASE
)
_BILIPOST_TAIL_RE = re.compile(r"<<<BILIPOST\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)

# 回复某条留言：三段式 <<<HEYPOST reply 帖子hash|对方评论开头|内容>>>
# 必须排在普通评论前面匹配，否则会被两段式规则先吃掉
_HEYPOST_REPLY_RE = re.compile(
    r"<<<HEYPOST\s+reply\s+([0-9A-Za-z]+)\s*\|\s*([^|>\n]+?)\s*\|\s*([^>\n]+?)\s*(?:>>>|＞＞＞)",
    re.IGNORECASE,
)
_HEYPOST_RE = re.compile(
    r"<<<HEYPOST\s+([0-9A-Za-z]+)\s*\|\s*([^>\n]+?)\s*(?:>>>|＞＞＞)", re.IGNORECASE
)
_HEYPOST_TAIL_RE = re.compile(r"<<<HEYPOST\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)


def _cfg(context: Any) -> Any:
    cfg = getattr(context, "config", None)
    if cfg is not None:
        return cfg
    try:
        from ...config import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


def _is_owner(context: Any, cfg: Any) -> bool:
    """这次是谁让她发的？不是主人就一律不认。

    两层名单都认：DL_SENPAI_OWNER_IDS（人设意义上的"主人"）和
    DL_SENPAI_AGENT_ADMIN_QQ（能碰高风险工具的管理员）。
    为什么要回退：实际部署里 OWNER_IDS 常常是空的（人设那块不依赖它），
    只认它会让写工具变成"谁都看不到"，等于功能永远打不开。
    """
    user_id = getattr(context, "user_id", None)
    if user_id is None:
        return False
    uid = str(user_id).strip()
    try:
        if cfg.is_owner(uid):
            return True
    except Exception:  # noqa: BLE001
        pass
    raw = str(getattr(cfg, "agent_admin_qq", "") or "")
    admins = {part.strip() for part in re.split(r"[,，;\s]+", raw) if part.strip()}
    return uid in admins


def _is_private(context: Any) -> bool:
    """私聊判定：群号为空就是私聊。"""
    return not str(getattr(context, "group_id", "") or "").strip()


def _visible_bilipost(context: Any) -> bool:
    cfg = _cfg(context)
    return bool(getattr(cfg, "bili_write_enable", False)) if cfg is not None else False


def _visible_qzone(context: Any) -> bool:
    cfg = _cfg(context)
    return bool(getattr(cfg, "qzone_enable", False)) if cfg is not None else False


def _visible_heypost(context: Any) -> bool:
    cfg = _cfg(context)
    return bool(getattr(cfg, "heybox_write_enable", False)) if cfg is not None else False


def _parse_qzone(text: str) -> tuple[str, list[ToolCall]]:
    calls: list[ToolCall] = []
    for match in _QZONE_RE.finditer(text or ""):
        action = (match.group(1) or "").strip().lower()
        argument = (match.group(2) or "").strip()
        if action == "draft" and not argument:
            continue
        calls.append(
            ToolCall(
                tool_name="qzone",
                arguments={"action": action, "text": argument},
                marker=QZONE_MARKER,
            )
        )
    cleaned = text or ""
    if calls:
        cleaned = _QZONE_RE.sub("", cleaned)
    else:
        cleaned = _QZONE_TAIL_RE.sub("", cleaned)
    return cleaned, calls


def _parse_heypost(text: str) -> tuple[str, list[ToolCall]]:
    calls: list[ToolCall] = []
    cleaned_text = text or ""
    for match in _HEYPOST_REPLY_RE.finditer(text or ""):
        link_id = (match.group(1) or "").strip()
        target = (match.group(2) or "").strip()
        body = (match.group(3) or "").strip()
        if not link_id or not target or not body:
            continue
        calls.append(
            ToolCall(
                tool_name="heybox_write",
                arguments={
                    "action": "reply",
                    "ref": link_id,
                    "target": target,
                    "text": body,
                },
                marker=HEYPOST_MARKER,
            )
        )
    if calls:
        cleaned_text = _HEYPOST_REPLY_RE.sub("", cleaned_text)
    for match in _HEYPOST_RE.finditer(cleaned_text):
        link_id = (match.group(1) or "").strip()
        body = (match.group(2) or "").strip()
        if not link_id or not body:
            continue
        calls.append(
            ToolCall(
                tool_name="heybox_write",
                arguments={"action": "comment", "ref": link_id, "text": body},
                marker=HEYPOST_MARKER,
            )
        )
    if calls:
        cleaned_text = _HEYPOST_RE.sub("", cleaned_text)
    else:
        cleaned_text = _HEYPOST_TAIL_RE.sub("", cleaned_text)
    return cleaned_text, calls


BILIPOST_PROMPT_TEXT = """你能在 B 站发一条评论，或者回别人给你的留言（要不要发由主人点头决定）。

用法：
    <<<BILIPOST BV1xx411c7mD|这条视频下说一句>>>
    <<<BILIPOST reply BV1xx411c7mD|123456|123789|回某条留言>>>

说明：
- reply 的两个数字是 root_id（楼层根评论）和 parent_id（对方那条），
  用 BILI inbox 看留言时会连着给你，照抄就行
- 一条一两句，像真人随手打的；不要总结视频、不要"感谢分享"
- 没有把握看过这个视频的内容就别夸它好，你不知道可以不说话
- 不要暴露 marker"""


def _parse_bilipost(text: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<BILIPOST ...>>>（顶层评论 / 楼中楼回复）。"""
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in _BILIPOST_REPLY_RE.finditer(text or ""):
        ref = (match.group(1) or "").strip()
        root_id = (match.group(2) or "").strip()
        parent_id = (match.group(3) or "").strip()
        body = (match.group(4) or "").strip()
        if not ref or not body:
            continue
        calls.append(
            ToolCall(
                tool_name="bili_write",
                arguments={
                    "action": "reply",
                    "ref": ref,
                    "root_id": int(root_id or 0),
                    "parent_id": int(parent_id or 0),
                    "text": body,
                },
                marker=BILIPOST_MARKER,
            )
        )
    if calls:
        cleaned = _BILIPOST_REPLY_RE.sub("", cleaned)
    for match in _BILIPOST_RE.finditer(cleaned):
        ref = (match.group(1) or "").strip()
        body = (match.group(2) or "").strip()
        if not ref or not body:
            continue
        calls.append(
            ToolCall(
                tool_name="bili_write",
                arguments={"action": "comment", "ref": ref, "text": body},
                marker=BILIPOST_MARKER,
            )
        )
    if calls:
        cleaned = _BILIPOST_RE.sub("", cleaned)
    else:
        cleaned = _BILIPOST_TAIL_RE.sub("", cleaned)
    return cleaned, calls


async def execute_bili_write(context: Any, call: ToolCall) -> ToolResult:
    """B站发评论 / 回留言。四道门同小黑盒：开关、主人、私聊、节奏护栏。"""
    from nonebot import logger

    from ... import bili as bl
    from ...web_login import WebLoginError
    from ...write_guard import check_can_comment, mark_reply_handled, record_comment

    args = call.arguments or {}
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="bili_write",
            tool_call_id=call.id,
            success=False,
            error="配置读不到，B站写入不可用",
            error_type="no_config",
        )
    if not _is_owner(context, cfg):
        return ToolResult(
            tool_name="bili_write",
            tool_call_id=call.id,
            success=False,
            output="【系统】B站评论只有主人点头才能发。",
            error="not_owner",
            error_type="permission_denied",
        )
    if not _is_private(context):
        return ToolResult(
            tool_name="bili_write",
            tool_call_id=call.id,
            success=False,
            output="【系统】要在群里发的话，先私聊我。",
            error="not_private",
            error_type="permission_denied",
        )

    action = str(args.get("action") or "comment").lower()
    ref = str(args.get("ref") or "").strip()
    body = str(args.get("text") or "")
    blocked = check_can_comment(
        ref, body, cfg, allow_same_link=(action == "reply"), platform="bili"
    )
    if blocked:
        return ToolResult(
            tool_name="bili_write",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】这条先别发：{blocked}",
            error="write_pace_blocked",
            error_type="rate_limited",
        )

    try:
        result = await bl.bili_comment(
            ref,
            body,
            cfg=cfg,
            root_id=int(args.get("root_id") or 0),
            parent_id=int(args.get("parent_id") or 0),
        )
    except (bl.BiliError, WebLoginError) as exc:
        return ToolResult(
            tool_name="bili_write",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】B站没发出去：{exc}",
            error=str(exc),
            error_type="comment_failed",
        )

    if action == "reply":
        mark_reply_handled(cfg, f"{args.get('parent_id')}", platform="bili")
    else:
        record_comment(str(result.get("bvid") or ref), body, cfg, platform="bili")
    logger.info(
        f"dl_senpai bili.{'replied' if action == 'reply' else 'commented'} "
        f"oid={result.get('oid')} rpid={result.get('rpid')}"
    )
    return ToolResult(
        tool_name="bili_write",
        tool_call_id=call.id,
        success=True,
        output=f"【系统】{'回复' if action == 'reply' else '评论'}发出去了：{body}",
        data=result,
    )


async def execute_qzone(context: Any, call: ToolCall) -> ToolResult:
    """草稿 / 列表 / 发布。四道门：开关、主人、私聊、每日配额。"""
    from nonebot import logger

    from ... import qzone as qz
    from ...quota import check_quota, refund_quota

    args = call.arguments or {}
    action = str(args.get("action") or "").lower()
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="qzone",
            tool_call_id=call.id,
            success=False,
            error="配置读不到，说说功能不可用",
            error_type="no_config",
        )
    if not _is_owner(context, cfg):
        return ToolResult(
            tool_name="qzone",
            tool_call_id=call.id,
            success=False,
            output="【系统】说说只有主人能让我发。",
            error="not_owner",
            error_type="permission_denied",
        )

    if action == "list":
        drafts = qz.list_drafts(cfg)
        if not drafts:
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=True,
                output="【系统】现在没有说说草稿。",
            )
        lines = ["【系统】说说草稿："]
        for draft in drafts[-5:]:
            state = "已发" if draft.published_at else "待发"
            lines.append(f"- {draft.draft_id}（{state}）：{draft.text[:40]}")
        return ToolResult(
            tool_name="qzone",
            tool_call_id=call.id,
            success=True,
            output="\n".join(lines),
            data={"count": len(drafts)},
        )

    if action == "draft":
        try:
            draft = qz.save_draft(str(args.get("text") or ""), cfg)
        except qz.QzoneError as exc:
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=False,
                output=f"【系统】这条说说存不下来：{exc}",
                error=str(exc),
                error_type="content_rejected",
            )
        logger.info(f"dl_senpai qzone.draft id={draft.draft_id} chars={len(draft.text)}")
        return ToolResult(
            tool_name="qzone",
            tool_call_id=call.id,
            success=True,
            output=(
                f"【系统】草稿存好了（{draft.draft_id}，{len(draft.text)} 字）。"
                "主人说「发」之前不会发出去。"
            ),
            data={"draft_id": draft.draft_id, "text": draft.text},
        )

    if action == "drop":
        # 只删没发的：发过的草稿是台账，留着才能对账
        target = str(args.get("text") or "").strip()
        if not target:
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=False,
                output="【系统】要删哪条？给个草稿编号，或者写 drop all。",
                error="missing_draft_id",
                error_type="bad_action",
            )
        if target.lower() in ("all", "全部"):
            removed = qz.clear_drafts(cfg)
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=True,
                output=f"【系统】清掉了 {removed} 条没发的草稿。",
                data={"removed": removed},
            )
        ok = qz.drop_draft(target, cfg)
        return ToolResult(
            tool_name="qzone",
            tool_call_id=call.id,
            success=bool(ok),
            output="【系统】删掉了。" if ok else "【系统】没找到这条没发的草稿。",
            error="" if ok else "draft_not_found",
            error_type="" if ok else "not_found",
        )

    if action == "publish":
        # 没开自动发时，必须主人在私聊里说 —— 群里喊一句「发」不算
        if not bool(getattr(cfg, "qzone_auto_post", False)) and not _is_private(context):
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=False,
                output="【系统】群里不发说说。要发请主人在私聊里说。",
                error="group_publish_blocked",
                error_type="permission_denied",
            )
        draft_id = str(args.get("text") or "").strip()
        draft = (
            next((d for d in qz.list_drafts(cfg, only_pending=True) if d.draft_id == draft_id), None)
            if draft_id
            else qz.latest_draft(cfg)
        )
        if draft is None:
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=False,
                output="【系统】没有待发的草稿。先 draft 一条。",
                error="no_draft",
                error_type="not_found",
            )

        allowed, used = await check_quota(
            "qzone", int(getattr(cfg, "qzone_daily_limit", 1) or 0), cfg
        )
        if not allowed:
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=False,
                output=f"【系统】今天说说的额度用完了（{used} 条）。",
                error="qzone_daily_limit",
                error_type="quota_exceeded",
            )

        from ...web_login import WebLoginError

        try:
            result = await qz.publish(draft.text, cfg)
        except (qz.QzoneError, WebLoginError) as exc:
            await refund_quota("qzone", cfg)
            return ToolResult(
                tool_name="qzone",
                tool_call_id=call.id,
                success=False,
                output=f"【系统】说说没发出去：{exc}",
                error=str(exc),
                error_type="publish_failed",
            )
        qz.mark_published(draft.draft_id, tid=result.tid, cfg=cfg)
        logger.info(
            f"dl_senpai qzone.published draft={draft.draft_id} tid={result.tid} chars={len(draft.text)}"
        )
        return ToolResult(
            tool_name="qzone",
            tool_call_id=call.id,
            success=True,
            output=f"【系统】发出去了：{draft.text}",
            data={"draft_id": draft.draft_id, "tid": result.tid},
        )

    return ToolResult(
        tool_name="qzone",
        tool_call_id=call.id,
        success=False,
        error=f"不认识的 QZONE 子命令：{action}",
        error_type="bad_action",
    )


async def execute_heybox_write(context: Any, call: ToolCall) -> ToolResult:
    """黑盒评论。同样四道门。"""
    from nonebot import logger

    from ... import heybox as hs
    from ...quota import check_quota, refund_quota

    args = call.arguments or {}
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="heybox_write",
            tool_call_id=call.id,
            success=False,
            error="配置读不到，评论功能不可用",
            error_type="no_config",
        )
    if not _is_owner(context, cfg):
        return ToolResult(
            tool_name="heybox_write",
            tool_call_id=call.id,
            success=False,
            output="【系统】评论只有主人点头才能发。",
            error="not_owner",
            error_type="permission_denied",
        )

    # 节奏护栏（间隔 / 同帖 / 文案重复）：不做道德审查，只防"看起来像脚本"
    from ...write_guard import check_can_comment, record_comment

    from ...write_guard import mark_reply_handled

    action = str(args.get("action") or "comment").lower()
    link_ref = str(args.get("ref") or "").strip()
    comment_text = str(args.get("text") or "")
    # 回复别人留言时，不做"同帖只能评一次"的检查（那是防刷屏，不是防对话），
    # 只保留间隔和文案去重——同一个人回你两次，你该回两次。
    blocked = check_can_comment(
        link_ref, comment_text, cfg, allow_same_link=(action == "reply")
    )
    if blocked:
        return ToolResult(
            tool_name="heybox_write",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】这条先别发：{blocked}",
            error="write_pace_blocked",
            error_type="rate_limited",
        )

    # 日限默认 0 = 不限；主人想硬卡也可以配
    allowed, used = await check_quota(
        "heybox_write", int(getattr(cfg, "heybox_comment_daily_limit", 0) or 0), cfg
    )
    if not allowed:
        return ToolResult(
            tool_name="heybox_write",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】今天的评论额度用完了（{used} 条）。",
            error="heybox_comment_daily_limit",
            error_type="quota_exceeded",
        )

    from ...web_login import WebLoginError

    try:
        if action == "reply":
            target = str(args.get("target") or "").strip()
            result = await hs.heybox_reply(link_ref, target, comment_text, cfg=cfg)
        else:
            result = await hs.heybox_comment(link_ref, comment_text, cfg=cfg)
    except (hs.HeyboxError, WebLoginError) as exc:
        await refund_quota("heybox_write", cfg)
        return ToolResult(
            tool_name="heybox_write",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】评论没发出去：{exc}",
            error=str(exc),
            error_type="comment_failed",
        )
    if action == "reply":
        mark_reply_handled(cfg, str(args.get("comment_id") or "") or comment_text[:12])
    else:
        record_comment(result.link_id, result.text, cfg)
    logger.info(
        f"dl_senpai heybox.{'replied' if action == 'reply' else 'commented'} "
        f"link={result.link_id} chars={len(result.text)}"
    )
    return ToolResult(
        tool_name="heybox_write",
        tool_call_id=call.id,
        success=True,
        output=f"【系统】{'回复' if action == 'reply' else '评论'}发出去了：{result.text}",
        data={"link_id": result.link_id, "text": result.text},
    )


def build_write_tools(cfg: Any = None) -> list[ToolDefinition]:
    """两个写入工具。默认都不注册（开关默认 false）。"""
    if cfg is None:
        try:
            from ...config import get_config

            cfg = get_config()
        except Exception:  # noqa: BLE001
            return []
    tools: list[ToolDefinition] = []
    if bool(getattr(cfg, "qzone_enable", False)):
        tools.append(
            ToolDefinition(
                name="qzone",
                description="写 QQ 空间说说草稿、查看草稿、按主人指令发布",
                marker=QZONE_MARKER,
                prompt_text=QZONE_PROMPT_TEXT,
                parser=_parse_qzone,
                executor=execute_qzone,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["draft", "list", "publish", "drop"],
                            "description": "draft=存草稿 list=看草稿 publish=发布 drop=删草稿",
                        },
                        "text": {
                            "type": "string",
                            "description": "草稿内容；publish 时可填 draft_id",
                        },
                    },
                    "required": ["action"],
                },
                permission="qzone.write",
                timeout=60.0,
                rate_limit=2.0,
                priority=55,
                continuation=Continuation.RETURN_TO_MODEL,
                failure_policy=FailurePolicy.RETURN_TO_MODEL,
                visibility=_visible_qzone,
                owner="write_layer",
                # 会留痕：不许重跑（重跑就是发第二条）
                side_effect=True,
            )
        )
    if bool(getattr(cfg, "heybox_write_enable", False)):
        tools.append(
            ToolDefinition(
                name="heybox_write",
                description="在小黑盒某个帖子下回一条评论（需主人点头）",
                marker=HEYPOST_MARKER,
                prompt_text=HEYPOST_PROMPT_TEXT,
                parser=_parse_heypost,
                executor=execute_heybox_write,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["comment"]},
                        "ref": {"type": "string", "description": "帖子分享链接里的 hash"},
                        "text": {"type": "string", "description": "评论内容"},
                    },
                    "required": ["action", "ref", "text"],
                },
                permission="heybox.write",
                timeout=120.0,
                rate_limit=3.0,
                priority=56,
                continuation=Continuation.RETURN_TO_MODEL,
                failure_policy=FailurePolicy.RETURN_TO_MODEL,
                visibility=_visible_heypost,
                owner="write_layer",
                side_effect=True,
            )
        )
    if bool(getattr(cfg, "bili_write_enable", False)):
        tools.append(
            ToolDefinition(
                name="bili_write",
                description="在 B 站发评论 / 回某条留言（需主人点头）",
                marker=BILIPOST_MARKER,
                prompt_text=BILIPOST_PROMPT_TEXT,
                parser=_parse_bilipost,
                executor=execute_bili_write,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["comment", "reply"]},
                        "ref": {"type": "string", "description": "BV 号或视频链接"},
                        "text": {"type": "string", "description": "评论内容"},
                        "root_id": {"type": "integer", "description": "楼层根评论 id（回复时）"},
                        "parent_id": {"type": "integer", "description": "被回复的评论 id（回复时）"},
                    },
                    "required": ["action", "ref", "text"],
                },
                permission="bili.write",
                # 走公开接口，一次请求就完事，比开浏览器快得多
                timeout=40.0,
                rate_limit=3.0,
                priority=57,
                continuation=Continuation.RETURN_TO_MODEL,
                failure_policy=FailurePolicy.RETURN_TO_MODEL,
                visibility=_visible_bilipost,
                owner="write_layer",
                side_effect=True,
            )
        )
    return tools
