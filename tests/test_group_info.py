"""群资源读取（群公告 / 群文件 / 群相册）的回归测试。

线上背景：学姐只能看到当前这条消息，问「群里公告写了什么」完全答不上来。
这里钉住三件事：接口名对不对、缺字段时能不能退化、渲染出来的文本模型能不能用。

NapCat 只给了泛化的 BaseResponse，没有具体字段说明，所以「缺字段」不是
理论风险而是常态——每个渲染器都必须扛得住空值和改名的键。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from dl_senpai.group_info import (
    KIND_ALBUM_MEDIA,
    KIND_ALBUMS,
    KIND_ESSENCE,
    KIND_FILES,
    KIND_FILE_CONTENT,
    KIND_NOTICE,
    SUPPORTED_KINDS,
    fetch_albums,
    fetch_essence,
    fetch_files,
    fetch_folder_files,
    fetch_notices,
    find_group_file,
    read_group_file,
    read_group_resource,
    render_album_media,
    render_albums,
    render_essence,
    render_files,
    render_notices,
)


class _FakeBot:
    """按接口名给响应的假 bot，顺带记录调用参数。"""

    def __init__(self, responses: dict[str, Any], *, raise_for: set[str] | None = None) -> None:
        self.responses = responses
        self.raise_for = raise_for or set()
        self.calls: list[dict] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append({"api": api, **kwargs})
        if api in self.raise_for:
            raise RuntimeError("api not supported: " + api)
        if api in self.responses:
            return self.responses[api]
        raise RuntimeError("api not supported: " + api)


# ---------------- 群公告 ----------------

async def test_fetch_notices_uses_underscore_api_first() -> None:
    # NapCat 的接口名带前导下划线，这是最容易写错的地方
    bot = _FakeBot({"_get_group_notice": [{"message": {"text": "本周六停课"}}]})
    notices = await fetch_notices(bot, 12345)
    assert [n["message"]["text"] for n in notices] == ["本周六停课"]
    assert bot.calls[0]["api"] == "_get_group_notice"


async def test_fetch_notices_falls_back_to_plain_api_name() -> None:
    # 只认带下划线的名字会漏掉旧实现，所以要留回退
    bot = _FakeBot({"get_group_notice": [{"message": {"text": "回退命中"}}]})
    notices = await fetch_notices(bot, 12345)
    assert [n["message"]["text"] for n in notices] == ["回退命中"]


async def test_fetch_notices_handles_wrapped_data() -> None:
    # 返回被 BaseResponse 包了一层
    bot = _FakeBot({"_get_group_notice": {"status": "ok", "data": [{"message": {"text": "包了一层"}}]}})
    notices = await fetch_notices(bot, 1)
    assert [n["message"]["text"] for n in notices] == ["包了一层"]


def test_render_notices_makes_readable_text() -> None:
    out = render_notices([
        {"message": {"text": "本周六停课"},
         "sender_id": "10001", "publish_time": 1700000000},
    ])
    assert "本周六停课" in out
    assert "10001" in out
    assert out.startswith("第1条")


def test_render_notices_marks_image_only_notice() -> None:
    # 纯图片公告没有文字，必须说清「看不到图里的字」，不能渲染成空白
    out = render_notices([{"message": {"images": [{"id": "a"}, {"id": "b"}]}}])
    assert "图片公告" in out and "2" in out


def test_render_notices_accepts_singular_image_key() -> None:
    # NapCat 文档里的示例用的是 message.image（单数），
    # 只认 images 会把图片公告误报成空公告。
    out = render_notices([{"message": {"image": [{"id": "a"}]}}])
    assert "图片公告" in out


def test_render_notices_shows_real_napcat_payload() -> None:
    # 照抄 NapCat 文档 Success 示例的结构，确保解析对准真实返回
    out = render_notices([{
        "notice_id": "notice_123",
        "sender_id": 123456,
        "publish_time": 1710000000,
        "message": {"text": "公告内容", "image": []},
    }])
    assert "公告内容" in out
    assert "123456" in out


def test_render_albums_shows_real_napcat_payload() -> None:
    # 照抄 get_qun_album_list 的 Success 示例
    out = render_albums([{
        "album_id": "album_1",
        "album_name": "测试相册",
        "cover_url": "http://example.com/cover.jpg",
        "create_time": 1734567890,
    }])
    assert "测试相册" in out


def test_render_notices_caps_and_mentions_remainder() -> None:
    notices = [{"message": {"text": "公告%d" % i}} for i in range(6)]
    out = render_notices(notices)
    assert "公告0" in out
    assert "公告3" not in out
    assert "还有 3 条更早的公告" in out


def test_render_notices_empty() -> None:
    assert render_notices([]) == ""


# ---------------- 群文件 ----------------

async def test_fetch_files_passes_group_and_count() -> None:
    bot = _FakeBot({"get_group_root_files": {"files": [{"file_name": "a.pdf"}]}})
    payload = await fetch_files(bot, 12345, file_count=10)
    assert payload["files"][0]["file_name"] == "a.pdf"
    assert bot.calls[0]["api"] == "get_group_root_files"
    assert bot.calls[0]["group_id"] == "12345"
    assert bot.calls[0]["file_count"] == 10


def test_render_files_shows_metadata_without_url() -> None:
    out = render_files({
        "files": [{"file_name": "计网复习.pdf", "file_size": 2 * 1024 * 1024,
                   "uploader_name": "小明", "upload_time": 1700000000}],
        "folders": [{"folder_name": "课件"}],
        "total": "3",
    })
    assert "计网复习.pdf" in out
    assert "2.0 MB" in out
    assert "小明" in out
    assert "课件" in out
    # 不能把下载链接塞进提示词：又长又临时，模型也用不上
    assert "http" not in out


def test_render_files_survives_missing_fields() -> None:
    # 字段全缺也不能抛，只是少显示点东西
    out = render_files({"files": [{}]})
    assert "未命名文件" in out


def test_render_files_tolerates_alternative_key_names() -> None:
    # 有的版本把列表叫 file_list / folder_list
    out = render_files({"file_list": [{"name": "b.txt"}], "folder_list": [{"name": "资料"}]})
    assert "b.txt" in out
    assert "资料" in out


def test_render_files_empty() -> None:
    assert render_files({"files": [], "folders": []}) == ""


# ---------------- 群相册 ----------------

async def test_fetch_albums_uses_pinyin_api_name() -> None:
    # 接口名是 get_qun_album_list（拼音），不是 get_group_album_list
    bot = _FakeBot({"get_qun_album_list": {"album_list": [{"album_name": "班会"}]}})
    albums = await fetch_albums(bot, 1)
    assert [a["album_name"] for a in albums] == ["班会"]
    assert bot.calls[0]["api"] == "get_qun_album_list"


async def test_fetch_albums_falls_back_to_group_name() -> None:
    bot = _FakeBot({"get_group_album_list": {"albums": [{"name": "备用名"}]}})
    albums = await fetch_albums(bot, 1)
    assert [a["name"] for a in albums] == ["备用名"]


def test_render_albums_shows_count_and_creator() -> None:
    out = render_albums([{"album_name": "班会", "total": "12",
                          "creator_name": "班长", "create_time": 1700000000}])
    assert "班会" in out
    assert "12 张" in out
    assert "班长" in out


def test_render_albums_empty() -> None:
    assert render_albums([]) == ""


def test_render_album_media_reports_count_not_links() -> None:
    # 相册内容只报数量和说明：图片本身走识图链路，不靠链接
    out = render_album_media([
        {"caption": "运动会", "uploader_name": "小明"},
        {"caption": ""},
    ])
    assert "2 张" in out
    assert "运动会" in out
    assert "小明" in out
    assert "http" not in out


def test_render_album_media_without_captions_is_just_a_count() -> None:
    # NapCat 实际返回的每张只有 media_id / url，没有说明和作者。
    # 这种情况逐条列"（无说明）"是纯噪音，应该只给张数。
    out = render_album_media([
        {"media_id": "m1", "url": "http://example.com/1.jpg"},
        {"media_id": "m2", "url": "http://example.com/2.jpg"},
    ])
    assert "2 张" in out
    assert "（无说明）" not in out
    assert "http" not in out


def test_render_album_media_empty() -> None:
    assert render_album_media([]) == ""


# ---------------- 群精华消息 ----------------

async def test_fetch_essence_uses_documented_api_name() -> None:
    # 接口名沿用 Go-CQHTTP 的 get_essence_msg_list，不是 get_group_essence
    bot = _FakeBot({"get_essence_msg_list": [{"content": "x"}]})
    items = await fetch_essence(bot, 7)
    assert len(items) == 1
    assert bot.calls[0]["api"] == "get_essence_msg_list"
    assert bot.calls[0]["group_id"] == "7"


async def test_fetch_essence_falls_back() -> None:
    bot = _FakeBot({"get_group_essence": [{"content": "y"}]})
    items = await fetch_essence(bot, 7)
    assert len(items) == 1


def test_render_essence_parses_segment_array() -> None:
    # NapCat 的 content 是**消息段数组**（和普通消息同构），不是字符串。
    # 按字符串处理会渲染成一段 repr，或者干脆空白。
    out = render_essence([{
        "content": [
            {"type": "text", "data": {"text": "注意："}},
            {"type": "at", "data": {"name": "小明"}},
            {"type": "text", "data": {"text": " 周六停课"}},
        ],
        "sender_nick": "班长",
        "operator_time": 1700000000,
    }])
    assert "注意：" in out
    assert "@小明" in out
    assert "周六停课" in out
    assert "班长" in out


def test_render_essence_marks_media_only_entries() -> None:
    # 纯图片精华没有文字。这里要的**不是**留空，而是给一个占位符，
    # 让模型知道"这条是张图"，而不是以为这条不存在。
    out = render_essence([{"content": [{"type": "image", "data": {}}]}])
    assert "[图片]" in out


def test_render_essence_caps_and_mentions_remainder() -> None:
    # 上限 20 条：显示第 1..20 条，第 21 条起截断并明说还剩多少
    items = [{"content": "精华%d" % i} for i in range(24)]
    out = render_essence(items)
    assert "1. 精华0" in out
    assert "20. 精华19" in out
    assert "21. 精华20" not in out
    assert "还有 4 条精华" in out


def test_render_essence_empty() -> None:
    assert render_essence([]) == ""


def test_render_essence_accepts_real_napcat_payload() -> None:
    # 照抄 NapCat 内嵌自测样例 GetGroupEssence 的结构
    out = render_essence([{
        "message_id": 123456, "sender_id": 123456, "sender_nick": "昵称",
        "operator_id": 123456, "operator_nick": "管理员",
        "operator_time": 1710000000, "content": [{"type": "text", "data": {"text": "精华内容"}}],
    }])
    assert "精华内容" in out
    assert "昵称" in out


# ---------------- 群文件正文 ----------------

async def test_fetch_folder_files_uses_folder_id() -> None:
    bot = _FakeBot({"get_group_files_by_folder": {"files": [{"file_name": "内页.pdf"}]}})
    files = await fetch_folder_files(bot, 1, "folder-9")
    assert [f["file_name"] for f in files] == ["内页.pdf"]
    assert bot.calls[0]["api"] == "get_group_files_by_folder"
    assert bot.calls[0]["folder_id"] == "folder-9"


async def test_find_group_file_prefers_exact_match() -> None:
    # 同时有「计网.pdf」和「计网复习.pdf」时，问「计网.pdf」应拿前者，
    # 而不是列表里第一个碰巧包含关键词的
    bot = _FakeBot({"get_group_root_files": {"files": [
        {"file_name": "计网复习.pdf", "file_id": "a"},
        {"file_name": "计网.pdf", "file_id": "b"},
    ]}})
    hit, folder = await find_group_file(bot, 1, "计网.pdf")
    assert hit is not None and hit["file_id"] == "b"
    assert folder == ""


async def test_find_group_file_falls_back_to_substring() -> None:
    bot = _FakeBot({"get_group_root_files": {"files": [
        {"file_name": "2026计网复习提纲.docx", "file_id": "a"},
    ]}})
    hit, _ = await find_group_file(bot, 1, "计网复习")
    assert hit is not None and hit["file_id"] == "a"


async def test_find_group_file_drills_into_folders() -> None:
    bot = _FakeBot({
        "get_group_root_files": {
            "files": [{"file_name": "无关.txt"}],
            "folders": [{"folder_id": "f1", "folder_name": "课件"}],
        },
        "get_group_files_by_folder": {"files": [{"file_name": "讲义.pdf", "file_id": "z"}]},
    })
    hit, folder = await find_group_file(bot, 1, "讲义")
    assert hit is not None and hit["file_id"] == "z"
    assert folder == "课件"


async def test_find_group_file_skips_broken_folder() -> None:
    # 某个文件夹读不到（无权限）不该让整次查找失败
    bot = _FakeBot(
        {
            "get_group_root_files": {
                "files": [],
                "folders": [{"folder_id": "bad", "folder_name": "坏"},
                            {"folder_id": "ok", "folder_name": "好"}],
            },
            "get_group_files_by_folder": {"files": [{"file_name": "找到.txt", "file_id": "k"}]},
        },
        raise_for={"__never__"},
    )

    async def _flaky(api: str, **kwargs: Any) -> Any:
        bot.calls.append({"api": api, **kwargs})
        if api == "get_group_files_by_folder" and kwargs.get("folder_id") == "bad":
            raise RuntimeError("no permission")
        if api in bot.responses:
            return bot.responses[api]
        raise RuntimeError("api not supported: " + api)

    bot.call_api = _flaky  # type: ignore[method-assign]
    hit, folder = await find_group_file(bot, 1, "找到")
    assert hit is not None and folder == "好"


async def test_find_group_file_returns_none_when_missing() -> None:
    bot = _FakeBot({"get_group_root_files": {"files": [{"file_name": "别的.txt"}]}})
    hit, folder = await find_group_file(bot, 1, "不存在的文件")
    assert hit is None and folder == ""


async def test_find_group_file_empty_query_is_inert() -> None:
    bot = _FakeBot({})
    assert await find_group_file(bot, 1, "   ") == (None, "")
    assert bot.calls == []


async def test_read_group_file_reports_missing_file() -> None:
    bot = _FakeBot({"get_group_root_files": {"files": []}})
    found, text = await read_group_file(bot, 1, "没这个")
    # 找不到也算"有结论"：应该照实说没有，而不是让模型自己猜
    assert found is True
    assert "没找到" in text


async def test_read_group_file_rejects_unsupported_suffix() -> None:
    bot = _FakeBot({"get_group_root_files": {"files": [
        {"file_name": "视频.mp4", "file_id": "v", "busid": 102},
    ]}})
    found, text = await read_group_file(bot, 1, "视频")
    assert found is True
    assert "读不了" in text
    # 不该白下载一个注定解析不出来的文件
    assert not any(c["api"] == "get_group_file_url" for c in bot.calls)


async def test_read_group_file_success(monkeypatch: Any) -> None:
    import dl_senpai.attachments as attachments

    bot = _FakeBot({
        "get_group_root_files": {"files": [
            {"file_name": "笔记.txt", "file_id": "f", "busid": 102, "file_size": 10},
        ]},
    })

    async def _fake_download(_bot: Any, _att: Any) -> bytes:
        return "这是正文".encode("utf-8")

    monkeypatch.setattr(attachments, "download_attachment", _fake_download)
    found, text = await read_group_file(bot, 1, "笔记")
    assert found is True
    assert "这是正文" in text
    assert "笔记.txt" in text


async def test_read_group_file_reports_download_failure(monkeypatch: Any) -> None:
    import dl_senpai.attachments as attachments

    bot = _FakeBot({"get_group_root_files": {"files": [
        {"file_name": "大文件.pdf", "file_id": "f", "busid": 102},
    ]}})

    async def _fail(_bot: Any, _att: Any) -> None:
        return None

    monkeypatch.setattr(attachments, "download_attachment", _fail)
    found, text = await read_group_file(bot, 1, "大文件")
    assert found is True
    assert "读不了" in text


async def test_read_group_file_reports_unparsable_content(monkeypatch: Any) -> None:
    import dl_senpai.attachments as attachments

    bot = _FakeBot({"get_group_root_files": {"files": [
        {"file_name": "扫描件.pdf", "file_id": "f", "busid": 102},
    ]}})

    async def _empty(_bot: Any, _att: Any) -> bytes:
        return b""

    monkeypatch.setattr(attachments, "download_attachment", _empty)
    found, text = await read_group_file(bot, 1, "扫描件")
    assert found is True
    assert "没抽出可读的文字" in text


async def test_read_group_file_without_file_id() -> None:
    bot = _FakeBot({"get_group_root_files": {"files": [{"file_name": "无ID.txt"}]}})
    found, text = await read_group_file(bot, 1, "无ID")
    assert found is True
    assert "文件 ID" in text


# ---------------- 统一入口 ----------------

async def test_read_group_resource_dispatches_each_kind() -> None:
    bot = _FakeBot({
        "_get_group_notice": [{"message": {"text": "公告正文"}}],
        "get_group_root_files": {"files": [{"file_name": "x.pdf"}]},
        "get_qun_album_list": {"album_list": [{"album_name": "相册A"}]},
        "get_group_album_media_list": {"media_list": [{"caption": "照片"}]},
        "get_essence_msg_list": [{"content": [{"type": "text", "data": {"text": "精华正文"}}]}],
    })
    for kind, expect in (
        (KIND_NOTICE, "公告正文"),
        (KIND_FILES, "x.pdf"),
        (KIND_ALBUMS, "相册A"),
        (KIND_ALBUM_MEDIA, "照片"),
        (KIND_ESSENCE, "精华正文"),
    ):
        found, text = await read_group_resource(bot, 1, kind, target="alb-1")
        assert found is True, kind
        assert expect in text, kind


async def test_read_group_resource_album_media_requires_target() -> None:
    bot = _FakeBot({})
    found, text = await read_group_resource(bot, 1, KIND_ALBUM_MEDIA)
    assert found is False and text == ""
    assert bot.calls == []


async def test_read_group_resource_file_content_requires_target() -> None:
    bot = _FakeBot({})
    found, text = await read_group_resource(bot, 1, KIND_FILE_CONTENT)
    assert found is False and text == ""
    assert bot.calls == []


async def test_read_group_resource_unknown_kind_is_inert() -> None:
    bot = _FakeBot({})
    found, text = await read_group_resource(bot, 1, "别的东西")
    assert found is False and text == ""
    assert bot.calls == []


async def test_read_group_resource_without_group_or_bot() -> None:
    assert await read_group_resource(None, 1, KIND_NOTICE) == (False, "")
    assert await read_group_resource(_FakeBot({}), "", KIND_NOTICE) == (False, "")


async def test_read_group_resource_reports_empty_as_not_found() -> None:
    # 群里确实没有公告 → 不是错误，但也不能谎报有内容
    bot = _FakeBot({"_get_group_notice": []})
    found, text = await read_group_resource(bot, 1, KIND_NOTICE)
    assert found is False and text == ""


def test_supported_kinds_match_the_documented_markers() -> None:
    # 名字里的数字会随迭代变成假话，所以只断言"集合恰好是这些"
    assert set(SUPPORTED_KINDS) == {
        KIND_NOTICE, KIND_FILES, KIND_ALBUMS,
        KIND_ALBUM_MEDIA, KIND_ESSENCE, KIND_FILE_CONTENT,
    }


# ---------------- 工具与执行器接线 ----------------

async def test_executor_rejects_unknown_kind() -> None:
    from dl_senpai.agent.tools.executors import execute_group_read
    from dl_senpai.agent.tools.models import ToolCall

    result = await execute_group_read(object(), ToolCall(id="c1", tool_name="group_read",
                                                         arguments={"kind": "不存在"}))
    assert result.success is False
    assert result.error_type == "bad_kind"


async def test_executor_returns_not_found_instead_of_guessing() -> None:
    from dl_senpai.agent.tools.executors import execute_group_read
    from dl_senpai.agent.tools.models import ToolCall

    context = SimpleNamespace(bot=_FakeBot({"_get_group_notice": []}), group_id="1")
    result = await execute_group_read(context, ToolCall(
        id="c2", tool_name="group_read", arguments={"kind": KIND_NOTICE}))
    assert result.success is False
    assert result.error_type == "empty_group_resource"


async def test_executor_feeds_content_back_to_model() -> None:
    from dl_senpai.agent.tools.executors import execute_group_read
    from dl_senpai.agent.tools.models import ToolCall

    bot = _FakeBot({"_get_group_notice": [{"message": {"text": "周六停课"}}]})
    context = SimpleNamespace(bot=bot, group_id="1")
    result = await execute_group_read(context, ToolCall(
        id="c3", tool_name="group_read", arguments={"kind": KIND_NOTICE}))
    assert result.success is True
    assert "周六停课" in result.output


def test_group_read_tool_is_registered_with_marker() -> None:
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executors import REAL_EXECUTORS

    tools = build_production_tools()
    target = [t for t in tools if t.name == "group_read"]
    assert len(target) == 1, "group_read 没注册或注册了多次"
    tool = target[0]
    assert tool.marker == "GROUP"
    assert tool.parser is not None
    # executor 必须是 REAL_EXECUTORS 里的同一个对象，不能是个占位
    assert tool.executor is REAL_EXECUTORS["group_read"]
    assert tool.prompt_text.strip(), "prompt_text 为空，模型不会知道这个工具"


async def test_parser_extracts_kind_and_target() -> None:
    from dl_senpai.agent.tools.builtin import _parse_group_read

    cleaned, calls = _parse_group_read("学姐看看\n<<<GROUP 相册内容 alb-9>>>\n谢谢")
    assert len(calls) == 1
    assert calls[0].arguments == {"kind": "相册内容", "target": "alb-9"}
    # 标记必须从正文里剥掉，否则会被当成回复内容发给群友
    assert "GROUP" not in cleaned
    assert "学姐看看" in cleaned and "谢谢" in cleaned


async def test_parser_handles_plain_kinds_and_leaks() -> None:
    from dl_senpai.agent.tools.builtin import _parse_group_read

    for text, kind in (("<<<GROUP 公告>>>", "公告"), ("<<<GROUP 文件>>>", "文件"),
                       ("<<<GROUP 相册>>>", "相册")):
        cleaned, calls = _parse_group_read(text)
        assert calls[0].arguments["kind"] == kind
        assert cleaned.strip() == ""


async def test_parser_keeps_spaces_in_file_keyword() -> None:
    # 文件名里带空格很常见，只取第一个词会找错文件
    from dl_senpai.agent.tools.builtin import _parse_group_read

    _, calls = _parse_group_read("<<<GROUP 文件内容 计网 复习 提纲>>>")
    assert calls[0].arguments == {"kind": "文件内容", "target": "计网 复习 提纲"}


def test_parser_ignores_empty_marker() -> None:
    from dl_senpai.agent.tools.builtin import _parse_group_read

    cleaned, calls = _parse_group_read("<<<GROUP>>>")
    assert calls == []
    assert "GROUP" not in cleaned


# ---------------- 全链路：marker → 解析 → dispatch → executor ----------------

async def test_full_chain_from_marker_to_executor() -> None:
    """从模型写出的 marker 一路走到 executor，中途任何一环断了都算失败。

    前面那些测试各自只覆盖一段；真正会出问题的是"接线"——
    parser 生成了 ToolCall、但 registry 里没这个工具，或者
    executor 没绑上，都会在线上表现为"模型调了但什么都没发生"。
    """
    from dl_senpai.agent.permissions import AllowAllPermissionService
    from dl_senpai.agent.tool_dispatch import SharedToolDispatcher
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for tool in build_production_tools():
        registry.register(tool)

    seen: list[tuple] = []
    original = registry.get("group_read").executor

    async def _spy(context: Any, call: Any) -> Any:
        seen.append((call.tool_name, dict(call.arguments or {})))
        return await original(context, call)

    registry.get("group_read").executor = _spy

    executor = ToolExecutor(registry, permissions=AllowAllPermissionService())
    dispatcher = SharedToolDispatcher(executor=executor)

    parser = registry.get("group_read").parser
    cleaned, calls = parser("好，学姐看看\n<<<GROUP 公告>>>")
    assert "GROUP" not in cleaned
    assert len(calls) == 1

    bot = _FakeBot({"_get_group_notice": [{"message": {"text": "周六停课"}}]})
    context = SimpleNamespace(bot=bot, group_id="1", interrupt=False, config=None)

    outcome = await dispatcher.dispatch(calls, context)
    assert seen == [("group_read", {"kind": "公告", "target": ""})]
    assert len(outcome.results) == 1
    assert outcome.results[0].success is True
    assert "周六停课" in outcome.results[0].output
