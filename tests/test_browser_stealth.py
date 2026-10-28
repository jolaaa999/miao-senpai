"""浏览器"像个人"这一层：反检测、持久 profile、人味操作、避让名单。

为什么值得钉：这层东西坏了不会报错，只会让站点更频繁地要人机验证——
而人机验证的表现是"读正文 0 字"，看起来像限流，很难反推到是浏览器指纹问题。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from dl_senpai import browser_stealth as bs
from dl_senpai import heybox_browse as hb


class Cfg:
    browser_profile_root = ""
    heybox_avoid_file = ""
    heybox_avoid_ttl_sec = 3600


def test_launch_args_disable_automation_flag() -> None:
    joined = " ".join(bs.LAUNCH_ARGS)
    assert "AutomationControlled" in joined
    # playwright 默认带的 --enable-automation 必须忽略，否则 webdriver 还是 true
    assert "--enable-automation" in bs.IGNORE_DEFAULT_ARGS


def test_stealth_js_covers_the_five_tells() -> None:
    js = bs.STEALTH_JS
    for marker in ("webdriver", "plugins", "languages", "chrome", "WebGLRenderingContext"):
        assert marker in js, marker


def test_profile_dir_is_per_site(tmp_path) -> None:
    cfg = Cfg()
    cfg.browser_profile_root = str(tmp_path)
    assert bs.profile_dir(cfg, "heybox") != bs.profile_dir(cfg, "douyin")
    assert bs.profile_dir(cfg, "heybox").name == "heybox"


def test_avoid_list_roundtrip(tmp_path) -> None:
    cfg = Cfg()
    cfg.heybox_avoid_file = str(tmp_path / "avoid.json")
    hb._avoid_mark(cfg, "abc123")
    assert "abc123" in hb._avoid_load(cfg)


def test_avoid_list_drops_expired(tmp_path) -> None:
    cfg = Cfg()
    cfg.heybox_avoid_ttl_sec = 10
    path = tmp_path / "avoid.json"
    cfg.heybox_avoid_file = str(path)
    path.write_text(json.dumps({"old": time.time() - 999, "new": time.time()}), encoding="utf-8")
    kept = hb._avoid_load(cfg)
    assert "new" in kept and "old" not in kept


def test_home_of_keeps_host_only() -> None:
    from dl_senpai.link_reader import _home_of

    assert _home_of("https://www.xiaoheihe.cn/app/bbs/link/abcdef") == "https://www.xiaoheihe.cn/"
    assert _home_of("nonsense") == ""
