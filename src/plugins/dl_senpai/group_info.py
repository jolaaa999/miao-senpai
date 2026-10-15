"""读取群里沉淀下来的公共资源：群公告、群文件、群相册。

为什么单独成模块：这几个都是「按群号问、拿到 id 再拉一层」的两段式接口，
而且 NapCat 官方文档只给了泛化的 BaseResponse、没有写明具体字段，所以解析
必须**防御式**——字段缺了就少显示一点，绝不能因为一个字段对不上就整次读取失败。

接口名有两个反直觉的地方，写在这里免得后来人改错：
    - 群公告是 _get_group_notice（带前导下划线）
    - 群相册列表是 get_qun_album_list（拼音 qun，不是 group）

三类资源全部**只读**：学姐可以看，但不能替群友发公告、传文件、改相册。
写入类接口一个都不碰。
"""

from __future__ import annotations

import time
from typing import Any

# 各类资源的显示上限。群公告常年只有几条，但接口可能返回历史全部；
# 群文件动辄上百个，全渲染会把上下文吃光，所以截断并显式说明。
MAX_NOTICES = 3
MAX_FILES = 40
MAX_FOLDERS = 20
MAX_ALBUMS = 20
MAX_MEDIA = 30
MAX_ESSENCE = 20
# 按关键词找文件时最多往下翻几层文件夹。群文件目录常见 1-2 层，
# 翻太深会变成"每次问一个文件都遍历整个目录树"，接口调用数不可控。
MAX_FOLDER_SCAN = 5
# 渲染文本总字数上限，与 forwards/attachments 同一量级的预算。
MAX_CHARS = 4000

# 工具支持读取的资源种类。executor 只认这几个值，
# 模型写出别的一律当参数错误处理（而不是猜一个近似的去查）。
KIND_NOTICE = "公告"
KIND_FILES = "文件"
KIND_ALBUMS = "相册"
KIND_ALBUM_MEDIA = "相册内容"
KIND_ESSENCE = "精华"
KIND_FILE_CONTENT = "文件内容"
SUPPORTED_KINDS = (
    KIND_NOTICE, KIND_FILES, KIND_ALBUMS, KIND_ALBUM_MEDIA,
    KIND_ESSENCE, KIND_FILE_CONTENT,
)


# ----------------------------------------------------------------
# 防御式取值小工具
# ----------------------------------------------------------------
#
# 这些函数存在的唯一理由：接口返回里有什么字段是**不可靠的**。
# 直接 data["files"][0]["file_name"] 这种写法，线上第一次字段改名就炸。
# 统一走这几个函数，缺字段时退化成默认值，读取整体仍然成功。

def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _first_list(container: dict[str, Any], *keys: str) -> list[Any]:
    """按顺序找第一个存在的列表字段。

    同一个概念在不同实现/版本里键名不一样（files / file_list，
    album_list / albums），所以给一串候选名，谁在就用谁。
    """
    for key in keys:
        found = _as_list(container.get(key))
        if found:
            return found
    return []


def _pick(container: Any, *keys: str, default: str = "") -> str:
    """按顺序取第一个非空的标量值，转成字符串。"""
    data = _as_dict(container)
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return default


def _unwrap(payload: Any, *keys: str) -> dict[str, Any]:
    """把 call_api 的返回剥到「装着数据的那个 dict」。

    NapCat 统一包一层 BaseResponse：{status, retcode, data, ...}，
    真正的内容在 data 里；data 有时又是 {"data": ...} 再包一层。
    这里剥到可能的层数，剩下的交给调用方按候选键取值。
    """
    current: Any = payload
    for _ in range(3):
        data = _as_dict(current)
        if not data:
            break
        if keys and any(key in data for key in keys):
            return data
        inner = data.get("data")
        if isinstance(inner, dict):
            current = inner
            continue
        # data 本身就是内容（比如公告是个 list）时停在这一层
        return data
    return _as_dict(current)


def _as_int(value: Any, default: int = 0) -> int:
    """把可能是字符串的数字转成 int；转不了就返回默认值。

    群文件接口的 file_size / busid 在不同版本里可能是字符串，
    直接 int() 会在字段格式变化时抛异常，拖垮整次读取。
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _fmt_size(value: Any) -> str:
    """字节数转成人能读的单位。取不到就返回空串，不显示 -1 B 这种噪音。"""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB")
    index = 0
    number = float(size)
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    if index == 0:
        return "%d %s" % (int(number), units[index])
    return "%.1f %s" % (number, units[index])


def _fmt_time(value: Any) -> str:
    """时间戳转 年-月-日。取不到就返回空串。

    为什么只到日期：群文件的精确秒对问答没意义，还会白占字符。
    """
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(stamp))
    except (OverflowError, OSError, ValueError):
        return ""


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _notice_text(notice: Any) -> str:
    """取一条公告的正文。

    go-cqhttp 把正文放在 message.text，NapCat 部分版本直接给 content/text。
    图片公告只有 images 没有文字，这时返回空串（由调用方标注「图片公告」）。
    """
    message = _as_dict(_as_dict(notice).get("message"))
    text = _pick(message, "text", "content")
    if text:
        return text
    return _pick(notice, "content", "text", "message_text")


# ----------------------------------------------------------------
# 群公告
# ----------------------------------------------------------------

async def fetch_notices(bot: Any, group_id: Any) -> list[Any]:
    """取群公告列表（原始条目，不渲染）。

    接口名带前导下划线是 NapCat 的写法；早期实现叫 get_group_notice，
    两个都试一遍，谁通用谁。
    """
    for api in ("_get_group_notice", "get_group_notice"):
        try:
            payload = await bot.call_api(api, group_id=str(group_id))
        except Exception:  # noqa: BLE001 — 换另一个接口名继续试
            continue
        notices = _as_list(payload)
        if not notices:
            # 返回可能是 {"data": [...]}
            notices = _as_list(_as_dict(payload).get("data"))
        if not notices:
            inner = _unwrap(payload)
            notices = _first_list(inner, "notices", "notice_list")
        if notices:
            return notices
    return []


def render_notices(notices: list[Any]) -> str:
    """把公告列表渲染成模型可读文本。"""
    if not notices:
        return ""
    lines: list[str] = []
    shown = notices[:MAX_NOTICES]
    for index, notice in enumerate(shown, start=1):
        body = _notice_text(notice)
        if not body:
            # NapCat 实际把图片放在 message.image（单数），早期实现用 images。
            # 两个都认，避免把"图片公告"误报成"空公告"。
            inner = _as_dict(_as_dict(notice).get("message"))
            images = _as_list(inner.get("images")) or _as_list(inner.get("image"))
            body = "（图片公告，%d 张图，学姐看不到图里的字）" % len(images) if images else "（空公告）"
        sender = _pick(notice, "sender_id", "sender_name", "sender")
        when = _fmt_time(_pick(notice, "publish_time", "create_time", "time"))
        meta = " ".join(part for part in (when, ("by " + sender) if sender else "") if part)
        head = "第%d条" % index + ("（" + meta + "）" if meta else "")
        lines.append(head + "\n" + body.strip())
    if len(notices) > MAX_NOTICES:
        lines.append("（群里还有 %d 条更早的公告没显示）" % (len(notices) - MAX_NOTICES))
    return _clip("\n\n".join(lines))


# ----------------------------------------------------------------
# 群文件
# ----------------------------------------------------------------

async def fetch_files(bot: Any, group_id: Any, *, file_count: int = MAX_FILES) -> dict[str, Any]:
    """取群文件根目录的文件与文件夹列表。"""
    payload = await bot.call_api(
        "get_group_root_files",
        group_id=str(group_id),
        file_count=int(file_count),
    )
    inner = _unwrap(payload, "files", "file_list", "folders", "folder_list")
    return {
        "files": _first_list(inner, "files", "file_list"),
        "folders": _first_list(inner, "folders", "folder_list"),
        "total": _pick(inner, "total_count", "file_count") or _pick(payload, "total_count"),
    }


def render_files(payload: dict[str, Any]) -> str:
    """把文件列表渲染成模型可读文本。

    只给元数据（名字/大小/上传者/时间），**不给下载链接**：
    链接是临时的、很长，而且模型拿到也没法直接读——真正读正文走 read_doc。
    """
    # 键名同样不写死：fetch_files 会归一化，但 render_files 也可能
    # 被别的调用方直接喂原始响应，多认几个名字不亏。
    files = _first_list(_as_dict(payload), "files", "file_list")
    folders = _first_list(_as_dict(payload), "folders", "folder_list")
    total = _pick(payload, "total")

    blocks: list[str] = []
    if folders:
        shown = folders[:MAX_FOLDERS]
        names: list[str] = []
        for folder in shown:
            name = _pick(folder, "folder_name", "name", default="未命名文件夹")
            # total_file_count 在 NapCat 的 folder 结构里是确认存在的字段，
            # 有它就顺手告诉模型这个文件夹里大概有多少东西。
            count = _pick(folder, "total_file_count", "file_count")
            names.append(name + ("（%s 个）" % count if count else ""))
        extra = ""
        if len(folders) > MAX_FOLDERS:
            extra = "（另有 %d 个文件夹）" % (len(folders) - MAX_FOLDERS)
        blocks.append("文件夹：" + "、".join(names) + extra)
    if files:
        shown_files = files[:MAX_FILES]
        lines: list[str] = []
        for index, item in enumerate(shown_files, start=1):
            name = _pick(item, "file_name", "name", default="未命名文件")
            size = _fmt_size(_pick(item, "file_size", "size"))
            uploader = _pick(item, "uploader_name", "uploader", "nickname")
            when = _fmt_time(_pick(item, "upload_time", "modify_time", "create_time"))
            meta = " ".join(part for part in (size, uploader, when) if part)
            lines.append("%d. %s%s" % (index, name, ("（" + meta + "）") if meta else ""))
        if len(files) > MAX_FILES:
            lines.append("…（还有 %d 个文件没显示）" % (len(files) - MAX_FILES))
        head = "文件共 %s 个" % (total or len(files))
        blocks.append(head + "：\n" + "\n".join(lines))
    if not blocks:
        return ""
    return _clip("\n\n".join(blocks))


# ----------------------------------------------------------------
# 群相册
# ----------------------------------------------------------------

async def fetch_albums(bot: Any, group_id: Any) -> list[Any]:
    """取群相册列表。

    注意接口名是 get_qun_album_list（拼音），不是 get_group_album_list——
    后者在这个实现里不存在。仍然留几个候选名，便于跨版本。
    """
    for api in ("get_qun_album_list", "get_group_album_list"):
        try:
            payload = await bot.call_api(api, group_id=str(group_id), attach_info="")
        except Exception:  # noqa: BLE001 — 换另一个接口名继续试
            continue
        inner = _unwrap(payload, "album_list", "albums")
        albums = _first_list(inner, "album_list", "albums", "list")
        if albums:
            return albums
    return []


def render_albums(albums: list[Any]) -> str:
    """把相册列表渲染成模型可读文本。"""
    if not albums:
        return ""
    lines: list[str] = []
    for index, album in enumerate(albums[:MAX_ALBUMS], start=1):
        name = _pick(album, "album_name", "name", default="未命名相册")
        count = _pick(album, "total", "photo_count", "count", "media_count")
        creator = _pick(album, "creator_name", "creator", "nickname")
        when = _fmt_time(_pick(album, "create_time", "modify_time"))
        meta = " ".join(
            part for part in (
                ("%s 张" % count) if count else "",
                creator,
                when,
            ) if part
        )
        lines.append("%d. %s%s" % (index, name, ("（" + meta + "）") if meta else ""))
    if len(albums) > MAX_ALBUMS:
        lines.append("…（还有 %d 个相册没显示）" % (len(albums) - MAX_ALBUMS))
    return _clip("\n".join(lines))


async def fetch_album_media(bot: Any, group_id: Any, album_id: str) -> list[Any]:
    """取某个相册里的媒体列表。"""
    payload = await bot.call_api(
        "get_group_album_media_list",
        group_id=str(group_id),
        album_id=str(album_id),
        attach_info="",
    )
    inner = _unwrap(payload, "media_list", "media", "photos")
    return _first_list(inner, "media_list", "media", "photos", "list")


def render_album_media(media: list[Any]) -> str:
    """把相册内容渲染成模型可读文本。

    这里**只报数量和每张的说明/时间**，不逐条贴图片链接：
    链接又长又临时，而学姐看到的图走的是识图链路，不是靠链接。
    这条限制在 persona 里也对模型讲清楚了，免得她硬编图里有什么。
    """
    media = [item for item in media if item is not None]
    if not media:
        return ""
    lines: list[str] = []
    for index, item in enumerate(media[:MAX_MEDIA], start=1):
        caption = _pick(item, "caption", "desc", "content", "text", "title")
        uploader = _pick(item, "uploader_name", "uploader", "nickname", "creator_name")
        when = _fmt_time(_pick(item, "upload_time", "create_time", "time"))
        meta = " ".join(part for part in (uploader, when) if part)
        if not caption and not meta:
            continue
        lines.append("%d. %s%s" % (index, caption or "（无说明）", ("（" + meta + "）") if meta else ""))
    head = "这个相册里有 %d 张照片" % len(media)
    if not lines:
        # NapCat 现在的响应里每张只有 media_id 和 url，没有说明也没有作者。
        # 逐条列"（无说明）"是纯噪音，直接给张数更有用。
        return _clip(head + "（学姐只能看到张数，看不到照片本身）。")
    if len(media) > MAX_MEDIA:
        lines.append("…（还有 %d 张没显示）" % (len(media) - MAX_MEDIA))
    return _clip(head + "：\n" + "\n".join(lines))


# ----------------------------------------------------------------
# 群精华消息
# ----------------------------------------------------------------

async def fetch_essence(bot: Any, group_id: Any) -> list[Any]:
    """取群精华消息列表。

    接口名是 get_essence_msg_list（不是 get_group_essence）——
    这是 Go-CQHTTP 时代的命名，NapCat 沿用。留一个候选名做兜底。
    """
    for api in ("get_essence_msg_list", "get_group_essence"):
        try:
            payload = await bot.call_api(api, group_id=str(group_id))
        except Exception:  # noqa: BLE001 — 换另一个接口名继续试
            continue
        items = _as_list(payload)
        if not items:
            items = _as_list(_as_dict(payload).get("data"))
        if not items:
            items = _first_list(_unwrap(payload), "messages", "essence_list")
        if items:
            return items
    return []


def render_essence(items: list[Any]) -> str:
    """把精华消息渲染成模型可读文本。

    正文是**消息段数组**（和普通消息同构），不是字符串，
    所以复用转发模块的 render_node_content 来解析，
    这样图片/表情会变成占位符而不是整段消失。
    """
    from .forwards import render_node_content

    lines: list[str] = []
    for index, item in enumerate(items[:MAX_ESSENCE], start=1):
        body = render_node_content(_as_dict(item).get("content"))
        if not body:
            body = "（这条没有文字，可能是图片或表情）"
        sender = _pick(item, "sender_nick", "sender_name", "sender", "sender_id")
        when = _fmt_time(_pick(item, "operator_time", "time", "create_time"))
        meta = " ".join(part for part in (sender, when) if part)
        lines.append("%d. %s%s" % (index, body, ("（" + meta + "）") if meta else ""))
    if len(items) > MAX_ESSENCE:
        lines.append("…（还有 %d 条精华没显示）" % (len(items) - MAX_ESSENCE))
    return _clip("\n".join(lines))


# ----------------------------------------------------------------
# 群文件正文
# ----------------------------------------------------------------

async def fetch_folder_files(bot: Any, group_id: Any, folder_id: str) -> list[Any]:
    """取某个文件夹下的文件列表。"""
    payload = await bot.call_api(
        "get_group_files_by_folder",
        group_id=str(group_id),
        folder_id=str(folder_id),
    )
    inner = _unwrap(payload, "files", "file_list")
    return _first_list(inner, "files", "file_list")


def _match_file(files: list[Any], query: str) -> dict[str, Any] | None:
    """在文件列表里按关键词找文件：先精确、再包含。

    精确优先是必要的：群里同时有"计网.pdf"和"计网复习.pdf"时，
    问"计网.pdf"应该拿到前者，而不是列表里第一个碰巧包含关键词的。
    """
    lowered = query.lower()
    for item in files:
        name = _pick(item, "file_name", "name")
        if name and name.lower() == lowered:
            return _as_dict(item)
    for item in files:
        name = _pick(item, "file_name", "name")
        if name and lowered in name.lower():
            return _as_dict(item)
    return None


async def find_group_file(bot: Any, group_id: Any, query: str) -> tuple[dict[str, Any] | None, str]:
    """按关键词找群文件，返回 (文件条目, 所在文件夹名)。

    先看根目录，没找到再逐层下钻（最多 MAX_FOLDER_SCAN 个文件夹）。
    子目录读取失败就当这个文件夹不存在，继续找下一个——
    某个文件夹没权限不该让整次查找失败。
    """
    query = (query or "").strip()
    if not query:
        return None, ""

    root = await fetch_files(bot, group_id)
    hit = _match_file(_as_list(root.get("files")), query)
    if hit:
        return hit, ""

    for folder in _as_list(root.get("folders"))[:MAX_FOLDER_SCAN]:
        folder_id = _pick(folder, "folder_id", "folder", "id")
        if not folder_id:
            continue
        try:
            sub_files = await fetch_folder_files(bot, group_id, folder_id)
        except Exception:  # noqa: BLE001 — 这个文件夹读不到就跳过
            continue
        hit = _match_file(sub_files, query)
        if hit:
            return hit, _pick(folder, "folder_name", "name")

    return None, ""


async def read_group_file(bot: Any, group_id: Any, query: str) -> tuple[bool, str]:
    """按关键词读群文件的正文，返回 (是否有结论, 文本)。

    复用 attachments 的整条链路（拿下载链接 → 下载 → 按扩展名抽取文本），
    不重新实现一遍。这里只负责"把群文件伪装成一个 Attachment"。

    注意返回值的第一项是"**有没有拿到可以照实说的结论**"，
    不是"成功与否"：文件找不到、格式不支持、下载失败都算"有结论"，
    因为这些都应该照实告诉群友，而不是让模型自己猜。
    """
    from .attachments import (
        SUPPORTED_HINT,
        Attachment,
        clip,
        download_attachment,
        extract_text,
    )

    try:
        hit, folder_name = await find_group_file(bot, group_id, query)
    except Exception as exc:  # noqa: BLE001 — 读目录失败按"找不到"处理
        return True, "学姐翻群文件的时候出错了（%s），这次没能读到。" % type(exc).__name__

    if not hit:
        return True, "群文件里没找到叫「%s」的东西。" % query

    name = _pick(hit, "file_name", "name", default="未命名文件")
    where = "（在「%s」文件夹里）" % folder_name if folder_name else ""
    file_id = _pick(hit, "file_id", "id")
    if not file_id:
        return True, "「%s」%s拿不到文件 ID，读不了正文。" % (name, where)

    attachment = Attachment(
        name=name,
        size=_as_int(_pick(hit, "file_size", "size")),
        file_id=file_id,
        busid=_as_int(_pick(hit, "busid", "bus_id")),
        group_id=str(group_id),
    )

    if not attachment.readable:
        return True, (
            "「%s」%s这个格式学姐读不了正文，只能读这些：%s。"
            % (name, where, SUPPORTED_HINT)
        )

    data = await download_attachment(bot, attachment)
    if data is None:
        return True, "「%s」%s下载失败或者太大了，学姐读不了。" % (name, where)

    text = extract_text(name, data)
    if not text.strip():
        return True, "「%s」%s里面没抽出可读的文字（可能是扫描件或空文件）。" % (name, where)

    return True, "【群文件「%s」%s的正文】\n%s" % (name, where, clip(text))


# ----------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------

async def read_group_resource(
    bot: Any,
    group_id: Any,
    kind: str,
    *,
    target: str = "",
) -> tuple[bool, str]:
    """按 kind 读取一种群资源，返回 (是否有内容, 渲染文本)。

    为什么返回二元组而不是直接抛异常：调用方（executor）需要区分
    「接口没内容」和「接口调用失败」——前者是正常结果（群里就没公告），
    后者要按 failure_policy 走。抛异常会把两者混在一起。
    """
    if bot is None or not str(group_id or "").strip():
        return False, ""

    if kind == KIND_NOTICE:
        text = render_notices(await fetch_notices(bot, group_id))
        return bool(text), text

    if kind == KIND_FILES:
        text = render_files(await fetch_files(bot, group_id))
        return bool(text), text

    if kind == KIND_ALBUMS:
        text = render_albums(await fetch_albums(bot, group_id))
        return bool(text), text

    if kind == KIND_ALBUM_MEDIA:
        if not target:
            return False, ""
        text = render_album_media(await fetch_album_media(bot, group_id, target))
        return bool(text), text

    if kind == KIND_ESSENCE:
        text = render_essence(await fetch_essence(bot, group_id))
        return bool(text), text

    if kind == KIND_FILE_CONTENT:
        if not target:
            return False, ""
        # read_group_file 自己会说明"找不到/读不了"，两种情况都算有结论
        return await read_group_file(bot, group_id, target)

    return False, ""


__all__ = [
    "KIND_ALBUM_MEDIA",
    "KIND_ALBUMS",
    "KIND_ESSENCE",
    "KIND_FILES",
    "KIND_FILE_CONTENT",
    "KIND_NOTICE",
    "MAX_CHARS",
    "SUPPORTED_KINDS",
    "fetch_album_media",
    "fetch_albums",
    "fetch_essence",
    "fetch_files",
    "fetch_folder_files",
    "fetch_notices",
    "find_group_file",
    "read_group_file",
    "read_group_resource",
    "render_album_media",
    "render_albums",
    "render_essence",
    "render_files",
    "render_notices",
]
