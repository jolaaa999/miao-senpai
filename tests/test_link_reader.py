"""读链接的围栏测试。

这一层的失败方式很贵：判错一次，群里任何人都能让她替自己请求内网
（http://192.168.1.1、http://127.0.0.1:27315 这些），或者她把站点名
当成正文去回答。所以下面钉的是「该拒的必须拒」和「残次品不能算读到」，
不是抓取效果本身——抓取效果靠真实链接验证，单测里不碰网络。
"""

from __future__ import annotations

import pytest

from dl_senpai.link_reader import (
    LinkDoc,
    extract_first_url,
    host_blocked_reason,
    render_brief,
    validate_url,
    _truncate,
)


class _Cfg:
    """最小配置替身：只放 link_reader 真正读的字段。"""

    link_read_allowed_domains = ""
    link_read_max_chars = 6000
    link_read_max_bytes = 2 * 1024 * 1024
    link_read_timeout = 20.0
    link_read_cache_ttl_sec = 0
    link_read_use_browser = False
    link_read_summary_enable = False
    link_read_summary_min_chars = 4000


# ---------------------------------------------------------------- 内网围栏


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:27315/",
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://localhost:8080/",
        "http://foo.local/",
        "http://metadata.google.internal/",
    ],
)
@pytest.mark.asyncio
async def test_private_targets_rejected(url: str) -> None:
    """内网/回环/元数据地址一律拒。

    群友能触发这条链路，所以这不是「最好拒」而是硬要求：
    漏一个就等于开放了内网扫描器。
    """
    assert await validate_url(url, _Cfg) != ""


@pytest.mark.parametrize("url", ["file:///C:/windows/win.ini", "ftp://x.com/a", "javascript:alert(1)"])
@pytest.mark.asyncio
async def test_non_http_schemes_rejected(url: str) -> None:
    assert await validate_url(url, _Cfg) != ""


def test_host_blocked_reason_messages_are_specific() -> None:
    """拒的时候要说清是哪一类，否则排查只能靠猜。"""
    assert "本机" in host_blocked_reason("localhost")
    assert "内网" in host_blocked_reason("192.168.0.1")
    assert host_blocked_reason("example.com") == ""


@pytest.mark.asyncio
async def test_allowed_domains_whitelist() -> None:
    """配了白名单就只认白名单（子域名算在内）。"""

    class _White(_Cfg):
        link_read_allowed_domains = "example.com, bilibili.com"

    assert await validate_url("http://example.com/a", _White) == ""
    # 解析失败（离线环境）不算拒——真正的拦截点是 DNS 与连接本身
    assert "白名单" in await validate_url("http://evil.cn/a", _White)


# ---------------------------------------------------------------- 链接提取


def test_extract_first_article_url() -> None:
    text = "看这个 https://a.com/post/1 挺有意思"
    assert extract_first_url(text) == "https://a.com/post/1"


def test_extract_skips_assets() -> None:
    """图片/压缩包不算「能读的链接」：当没看见，比读失败后回一句「读不到」好。"""
    assert extract_first_url("图 https://a.com/a.jpg 看看") == ""
    assert extract_first_url("包 https://a.com/x.zip") == ""
    assert extract_first_url("文字 https://a.com/x.zip 和 https://a.com/post/2") == "https://a.com/post/2"


def test_extract_keeps_query_string() -> None:
    assert extract_first_url("https://a.com/s?q=1&b=2") == "https://a.com/s?q=1&b=2"


def test_extract_none() -> None:
    assert extract_first_url("没有链接") == ""


# ---------------------------------------------------------------- 截断与提示


def test_truncate_keeps_head_and_tail() -> None:
    """只留开头会让她的判断系统性偏向前半篇，所以尾巴必须留。"""
    body = "A" * 5000 + "TAILKEY" + "B" * 100
    out, truncated = _truncate(body, 1000)
    assert truncated is True
    assert "TAILKEY" in out
    assert len(out) <= 1100


def test_truncate_noop_when_short() -> None:
    out, truncated = _truncate("短短一句", 1000)
    assert out == "短短一句"
    assert truncated is False


def test_render_brief_failure_tells_model_not_to_guess() -> None:
    """读不到时必须明确说「别凭 URL 猜」，否则模型会顺着域名编。"""
    doc = LinkDoc(url="https://a.com/x", error="timeout")
    text = render_brief(doc)
    assert "读链接失败" in text
    assert "不要凭 URL 猜" in text


def test_render_brief_ok_marks_truncation() -> None:
    doc = LinkDoc(
        url="https://a.com/x",
        title="标题",
        text="正文内容",
        source="http",
        total_chars=99999,
        truncated=True,
    )
    text = render_brief(doc, focus="想知道价格")
    assert "标题" in text
    assert "已截断" in text
    assert "想知道价格" in text
