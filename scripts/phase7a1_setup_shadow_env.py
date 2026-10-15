"""向 .env 追加 Phase 7A.1 的 Shadow 实验配置。

用 Python 而不是 PowerShell，避免编码问题（此前的 BOM 教训）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

SALT = "eR8OgzXZa0uk8VbqM3L9OpEWnnF8NYvWmBssrf3Mubc"

BLOCK = f"""
# ===== Phase 7A.1 真实 Shadow 实验（人工添加）=====
# SHADOW 语义：Legacy 真正执行并回复（群友体验不变），
#             Runtime 只做 dry-run 推演，零副作用。
# VALIDATION=1 开启采集；数据写入 data/validation/（与业务数据分离）。
# SALT 用于 HMAC 脱敏真实 QQ 号/群号 —— 换盐会导致历史数据无法关联。
DL_SENPAI_AGENT_MODE=shadow
DL_SENPAI_VALIDATION=1
DL_SENPAI_VALIDATION_SALT={SALT}
"""


def main() -> int:
    src = ENV.read_text(encoding="utf-8")

    if "DL_SENPAI_AGENT_MODE=shadow" in src:
        print("已存在 shadow 配置，跳过")
        return 0

    if not src.endswith("\n"):
        src += "\n"
    ENV.write_text(src + BLOCK, encoding="utf-8")

    print("已追加配置")
    print("BOM:", ENV.read_bytes()[:3] == b"\xef\xbb\xbf")
    print(f"行数: {len(ENV.read_text(encoding='utf-8').splitlines())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
