"""提交前自检：仓库里有没有漏进去的密钥、真实群号、本机路径。

搬自 ai-companion-kit 的 scripts/check.mjs 第 108-132 行。它的用法值得抄：
不开服务、不连网、不调模型，一条命令扫完整个仓库。

跟 src/plugins/dl_senpai/validation/privacy.py 不是一回事，别混：
    privacy.py  运行期把用户 QQ 号脱敏成 HMAC 匿名 id（防的是日志泄漏）；
    本脚本      提交前扫仓库文件（防的是把真实群号/密钥推到公开仓库）。

为什么需要：测试里为了「像真的」会写真实群号，比如 980229149。
平时无害，但一旦仓库开源、或推到公开 fork，这些就跟着出去了。

用法：
    python scripts/check_leaks.py            # 扫已跟踪的文件
    python scripts/check_leaks.py --all      # 连未跟踪的文件一起扫

退出码：发现密钥或私钥 → 1；只有 QQ 号/路径这类提示 → 0。
为什么密钥硬失败、群号只提示：密钥泄漏立刻要处理；而测试里的假 QQ 号
大多无害，全都拦下来只会让人把这个脚本关掉。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只扫这些后缀：二进制和压缩包扫了也没意义
_SCAN_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yml", ".yaml",
    ".toml", ".ini", ".ps1", ".bat", ".example", ".cfg",
}

# 硬失败：真出了就是事故
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "疑似 API key（sk- 开头）"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "疑似 GitHub token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "疑似 AWS key"),
]

# 只提示：测试里用真实群号很常见，不值得一律拦下
_HINT_PATTERNS = [
    (re.compile(r"(?<!\d)\d{9,11}(?!\d)"), "疑似 QQ 号 / 群号（9~11 位数字）"),
    (re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}"), "疑似本机用户目录路径"),
]

# 合成 id 与文档占位，命中了不必报
_ID_ALLOWLIST = {
    "10000", "10001", "10002", "10003", "10004", "10005",
    "12345", "123456", "1234567", "12345678", "123456789",
    "100000000", "1000000000", "99999999999",
    "1791409879392", "1791407913658", "1791412928131",
}

# 这些文件整体跳过：本文件自己写着这些正则，配置模板/README 里有示例值
_SKIP_PATHS = {
    "scripts/check_leaks.py",
    ".env.example",
    "README.md",
}

# 这些目录整体跳过：第三方数据/依赖，不归本项目管
_SKIP_PREFIXES = (
    "data/",
    ".tmp_ref/",
    "node_modules/",
    ".venv/",
    "MirrorFlow/",  # 外部数据集，里面全是演示用假号
)

# 明显是人造的假值：测试夹具里写 sk-abcdefghijklmnopqrst 是正常做法，
# 一律当事故报只会让这个脚本被关掉。
_FAKE_VALUE_HINTS = (
    "abcdefghij",
    "1234567890",
    "xxxxxxxx",
    "your-key",
    "placeholder",
    "example",
)


def _looks_like_fake(value: str) -> bool:
    """肉眼可辨的假值：长度不足、含连续占位串、或全是同一个字符。"""
    low = value.lower()
    if any(hint in low for hint in _FAKE_VALUE_HINTS):
        return True
    # sk-abababababab... 这种重复模式也是假的
    body = low[3:] if low.startswith("sk-") else low
    return len(set(body)) <= 4


def _tracked_files(include_untracked: bool) -> list[Path]:
    """取要扫的文件。优先用 git 清单——那才是「会被推出去的东西」。"""
    args = ["git", "ls-files"]
    if include_untracked:
        args += ["--cached", "--others", "--exclude-standard"]
    try:
        out = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except Exception:
        # 没有 git（或不是仓库）时退回全量遍历，功能不至于完全不可用
        out = "\n".join(
            str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()
        )
    files: list[Path] = []
    for line in out.splitlines():
        rel = line.strip().replace("\\", "/")
        if not rel or rel in _SKIP_PATHS:
            continue
        if rel.startswith(_SKIP_PREFIXES):
            continue
        path = ROOT / rel
        if path.suffix.lower() in _SCAN_SUFFIXES and path.is_file():
            files.append(path)
    return files


def scan(include_untracked: bool = False) -> tuple[list[str], list[str]]:
    """返回 (致命项, 提示项)，每项形如「路径:行号 说明 → 命中内容」。"""
    fatal: list[str] = []
    hints: list[str] = []

    for path in _tracked_files(include_untracked):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in _SECRET_PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                if _looks_like_fake(m.group(0)):
                    hints.append(f"{rel}:{lineno} {label}（像是测试假值）→ {m.group(0)[:24]}")
                else:
                    fatal.append(f"{rel}:{lineno} {label} → {m.group(0)[:24]}")
            for pattern, label in _HINT_PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                if m.group(0) in _ID_ALLOWLIST:
                    continue
                hints.append(f"{rel}:{lineno} {label} → {m.group(0)[:32]}")
    return fatal, hints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_leaks", description="提交前自检：密钥 / 群号 / 本机路径"
    )
    parser.add_argument("--all", action="store_true", help="连未跟踪的文件一起扫")
    args = parser.parse_args(argv)

    fatal, hints = scan(include_untracked=args.all)

    if fatal:
        print(f"✗ 发现 {len(fatal)} 处疑似密钥（必须处理）")
        for item in fatal:
            print("   " + item)
    else:
        print("✓ 没有发现密钥 / 私钥")

    if hints:
        # 按文件聚合：几十上百条逐个打印没人看得完，按文件给数量才扫得动
        by_file: dict[str, int] = {}
        for item in dict.fromkeys(hints):
            path = item.split(":", 1)[0]
            by_file[path] = by_file.get(path, 0) + 1
        total = sum(by_file.values())
        print(f"⚠ {total} 处提示（不拦，但开源前建议看一眼）")
        for path, count in sorted(by_file.items(), key=lambda kv: -kv[1]):
            print(f"   {path}  ×{count}")
        print("   看具体行号：python scripts/check_leaks.py --all | Select-String 上面列出的文件")
    else:
        print("✓ 没有发现可疑群号 / 本机路径")

    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
