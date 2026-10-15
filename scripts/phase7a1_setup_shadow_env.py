"""向 .env 追加 Phase 7A.1 的 Shadow 实验配置。

用 Python 而不是 PowerShell，避免编码问题（此前的 BOM 教训）。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

# 盐不写死在脚本里：仓库是公开的，把盐提交上去等于连脱敏数据的钥匙一起交出去。
# 下面这段是要写进 .env 的内容，盐要么复用 .env 里已有的（历史脱敏数据才对得上号），
# 要么现场随机生成。占位符在 main 里替换。
SALT_PLACEHOLDER = "__VALIDATION_SALT__"

BLOCK = """
# ===== Phase 7A.1 真实 Shadow 实验（人工添加）=====
# SHADOW 语义：Legacy 真正执行并回复（群友体验不变），
#             Runtime 只做 dry-run 推演，零副作用。
# VALIDATION=1 开启采集；数据写入 data/validation/（与业务数据分离）。
# SALT 用于 HMAC 脱敏真实 QQ 号/群号 —— 换盐会导致历史数据无法关联。
DL_SENPAI_AGENT_MODE=shadow
DL_SENPAI_VALIDATION=1
DL_SENPAI_VALIDATION_SALT=__VALIDATION_SALT__
"""


def _existing_salt(src: str) -> str:
    """复用 .env 里已有的盐：换盐会让历史脱敏数据对不上号。"""
    m = re.search(r"^DL_SENPAI_VALIDATION_SALT\s*=\s*(\S+)", src, re.M)
    return m.group(1) if m else ""


def main() -> int:
    src = ENV.read_text(encoding="utf-8")

    if "DL_SENPAI_AGENT_MODE=shadow" in src:
        print("已存在 shadow 配置，跳过")
        return 0

    if not src.endswith("\n"):
        src += "\n"
    # 复用已有盐，避免历史脱敏数据对不上号；没有才生成（随机，不写死）
    salt = _existing_salt(src) or secrets.token_hex(24)
    ENV.write_text(src + BLOCK.replace(SALT_PLACEHOLDER, salt), encoding="utf-8")

    print("已追加配置")
    print("盐来源:", "复用 .env 已有值" if _existing_salt(src) else "本次随机生成")
    print("BOM:", ENV.read_bytes()[:3] == b"\xef\xbb\xbf")
    print(f"行数: {len(ENV.read_text(encoding='utf-8').splitlines())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
