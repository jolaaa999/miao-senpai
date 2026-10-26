"""仓库卫生守卫：源码里不许有真密钥，.env 不许被 git 跟踪。

跟 scripts/check_leaks.py 分工不同，别混：
    脚本  提交前跑一遍，给完整报告（含群号、本机路径这类提示项）；
    本文件 只是把其中「永远不该发生」的部分钉成测试——
          密钥泄漏和 .env 被提交，这两件事一旦发生就是事故。

为什么只钉这两条、不去管群号：测试里用真实群号很常见（
test_phase5_replay.py 甚至专门拿它当黑名单），全都拦下来只会
让人把这个测试删掉，反而失去守卫作用。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 真出了就是事故的模式
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]

# 肉眼可辨的假值：测试夹具里写 sk-abcdefghijklmnopqrst 是正常做法
_FAKE_HINTS = ("abcdefghij", "1234567890", "xxxxxxxx", "your-key", "placeholder", "example")

# 本文件自己和扫描脚本里都写着这些正则，扫了必然自伤
_SKIP = {"scripts/check_leaks.py", "tests/test_no_secrets.py"}


def _tracked(patterns: tuple[str, ...]) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", *patterns],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — 没有 git 就跳过，不让测试因环境失败
        return []
    files = []
    for line in out.splitlines():
        rel = line.strip().replace("\\", "/")
        if rel and rel not in _SKIP and not rel.startswith(("data/", "MirrorFlow/")):
            files.append(ROOT / rel)
    return files


def _looks_fake(value: str) -> bool:
    low = value.lower()
    if any(hint in low for hint in _FAKE_HINTS):
        return True
    body = low[3:] if low.startswith("sk-") else low
    return len(set(body)) <= 4


def test_no_real_secrets_in_tracked_files() -> None:
    """源码/文档里不许出现真 API key、token、私钥。"""
    leaks: list[str] = []
    for path in _tracked(("*",)):
        if path.suffix.lower() not in {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".ini", ".ps1"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in _SECRET_PATTERNS:
                m = pattern.search(line)
                if m and not _looks_fake(m.group(0)):
                    rel = str(path.relative_to(ROOT)).replace("\\", "/")
                    leaks.append(f"{rel}:{lineno} → {m.group(0)[:24]}")
    assert not leaks, "仓库里出现疑似真密钥，必须处理：\n" + "\n".join(leaks)


def test_env_is_not_tracked() -> None:
    """.env 里有机器人 token、API key、真实群号，绝不能进版本库。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", ".env"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001
        return  # 没有 git 就不判
    assert not out.strip(), f".env 被 git 跟踪了：{out.strip()}"


def test_env_is_gitignored() -> None:
    """.gitignore 必须挡住 .env —— 光现在没跟踪不够，以后 add . 也不能进来。"""
    gitignore = ROOT / ".gitignore"
    assert gitignore.is_file(), "缺少 .gitignore"
    lines = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    }
    assert ".env" in lines, ".gitignore 缺少 .env"
