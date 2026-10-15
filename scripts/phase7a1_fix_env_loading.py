"""把 config_env 接入读 os.environ 的模块（Phase 7A.1 缺陷修复）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"


def patch(path: Path, pairs: list[tuple[str, str]], import_line: str = "") -> None:
    src = path.read_text(encoding="utf-8")
    applied = 0
    for old, new in pairs:
        if old in src:
            src = src.replace(old, new)
            applied += 1
    if import_line and import_line not in src:
        anchor = "from __future__ import annotations"
        src = src.replace(anchor, anchor + "\n\n" + import_line, 1)
    path.write_text(src, encoding="utf-8")
    remaining = src.count("os.environ")
    print(f"  {path.name}: 替换 {applied} 处, 剩余 os.environ={remaining}, "
          f"BOM={path.read_bytes()[:3] == b'\\xef\\xbb\\xbf'}")


def main() -> int:
    patch(
        PKG / "agent" / "mode.py",
        [
            (
                'raw = (explicit or os.environ.get(ENV_MODE, "")).strip().lower()',
                'raw = (explicit or get_env(ENV_MODE, "")).strip().lower()',
            ),
            (
                'if os.environ.get(ENV_SHADOW_LEGACY, "").strip().lower() in {"1", "true", "yes", "on"}:',
                "if get_flag(ENV_SHADOW_LEGACY):",
            ),
            (
                'raw = os.environ.get(ENV_CANARY_GROUPS, "").strip()',
                'raw = get_env(ENV_CANARY_GROUPS, "").strip()',
            ),
        ],
        "from ..config_env import get_env, get_flag",
    )

    patch(
        PKG / "validation" / "store.py",
        [
            (
                'override = os.environ.get("DL_SENPAI_VALIDATION_DIR")',
                'override = get_env("DL_SENPAI_VALIDATION_DIR", "")',
            ),
            (
                '            enabled = (os.environ.get("DL_SENPAI_VALIDATION") or "").strip() in {\n'
                '                "1", "true", "yes", "on",\n'
                '            }',
                '            enabled = get_flag("DL_SENPAI_VALIDATION")',
            ),
        ],
        "from ..config_env import get_env, get_flag",
    )

    patch(
        PKG / "validation" / "privacy.py",
        [
            (
                'return (os.environ.get(_SALT_ENV) or DEFAULT_SALT).encode("utf-8")',
                'return (get_env(_SALT_ENV, "") or DEFAULT_SALT).encode("utf-8")',
            ),
            (
                '    return (os.environ.get("DL_SENPAI_DEBUG_CONTENT") or "").strip() in {\n'
                '        "1", "true", "yes", "on",\n'
                '    }',
                '    return get_flag("DL_SENPAI_DEBUG_CONTENT")',
            ),
        ],
        "from ..config_env import get_env, get_flag",
    )

    patch(
        PKG / "validation" / "cost.py",
        [],
        "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
