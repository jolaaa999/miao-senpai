"""快速自检：语法 → 导入 → 冒烟不变量。不连网、不调模型、不起服务。

搬自 ai-companion-kit 的 scripts/check.mjs（它做 syntax / import / smoke 三段）。
为什么需要：语法错误和导入错误现在要等到重启 bot 才炸，而重启一次要
几十秒、还会打断线上说话。这个脚本两秒钟就能拦住其中绝大多数。

跟另外两个脚本的分工，别混：
    check.py        代码能不能跑起来（语法/导入/关键不变量）
    check_leaks.py  仓库里有没有漏出去的密钥和真实群号
    dryrun.py       跑一次真实链路看语气（会调模型）

用法：
    python scripts\\check.py
    python scripts\\check.py --list     # 只列检查项，不执行

退出码：0 全过；1 有失败项。
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# 这些模块导入时会起后台线程/占用端口，自检不该触发它们。
# 目前没有，留个位置：加名字进来即可。
_IMPORT_SKIP = set()


class _Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, label: str, detail: str) -> None:
        self.failures.append(f"{label}: {detail}")

    def note(self, text: str) -> None:
        self.notes.append(text)


def check_syntax(res: _Result) -> None:
    """① 语法：把仓库里的 .py 全编译一遍（不写 .pyc）。"""
    targets = [
        *sorted((ROOT / "src").rglob("*.py")),
        *sorted((ROOT / "scripts").rglob("*.py")),
        *sorted((ROOT / "tests").rglob("*.py")),
    ]
    bad = 0
    for path in targets:
        try:
            source = path.read_text(encoding="utf-8")
            # 就地编译：比 py_compile 少写一堆 __pycache__，扫完即走
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            bad += 1
            rel = path.relative_to(ROOT)
            res.fail("语法", f"{rel}:{exc.lineno} {exc.msg}")
        except Exception as exc:  # noqa: BLE001 — 编码等读文件问题也要报
            bad += 1
            res.fail("语法", f"{path.relative_to(ROOT)} 读取失败: {exc}")
    res.note(f"语法：编译 {len(targets)} 个文件，失败 {bad} 个")


def check_imports(res: _Result) -> None:
    """② 导入：遍历插件包下所有子模块，逐个 import。

    用 walk_packages 而不是手写清单：以后新增模块自动被覆盖，
    清单会忘，自动化不会。
    """
    import plugins.dl_senpai as pkg

    count = 0
    for info in pkgutil.walk_packages(pkg.__path__, prefix="plugins.dl_senpai."):
        name = info.name
        if name in _IMPORT_SKIP or any(name.endswith(s) for s in _IMPORT_SKIP):
            continue
        count += 1
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            # 只留最后一帧的定位信息，整段 traceback 太长反而看不清
            tb = traceback.format_exc().strip().splitlines()
            where = next(
                (ln.strip() for ln in reversed(tb) if "dl_senpai" in ln),
                type(exc).__name__,
            )
            res.fail("导入", f"{name} -> {type(exc).__name__}: {exc} | {where}")
    res.note(f"导入：尝试 {count} 个子模块")


def check_smoke(res: _Result) -> None:
    """③ 冒烟：几条「一坏就说明结构变了」的不变量。"""
    from plugins.dl_senpai.brevity import brevity_hint_text
    from plugins.dl_senpai.config import get_config
    from plugins.dl_senpai.persona import SYSTEM_PROMPT

    cfg = get_config()

    # 人设不能空、也不能短到只剩一句话（守卫测试同样断言 >200）
    if len(SYSTEM_PROMPT) < 200:
        res.fail("冒烟", f"人设过短（{len(SYSTEM_PROMPT)} 字），可能被误删")

    # 长度纪律必须在，且必须能在提示词最末尾兜住长度
    hint = brevity_hint_text()
    if not hint or "句" not in hint:
        res.fail("冒烟", "长度纪律文案缺失（brevity.py）")

    # 关键配置项读得到；缺了会导致运行时静默走默认值
    for name in ("temperature", "max_tokens", "bubble_max", "own_life_enable"):
        if not hasattr(cfg, name):
            res.fail("冒烟", f"配置项缺失: {name}")

    # 去重与攒批的默认值：关掉会退化成「重复回复」和「追着每条答一句」
    if not bool(getattr(cfg, "dedupe_enable", False)):
        res.fail("冒烟", "dedupe_enable 默认应为 True")
    if not bool(getattr(cfg, "batch_enable", False)):
        res.fail("冒烟", "batch_enable 默认应为 True")

    # 提示词拼装：总长度是最容易被忽视的指标，打出来好观察
    try:
        from plugins.dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest

        built = PromptBuilder().build(PromptRequest(brevity_hint=hint))
        text = getattr(built, "text", "") or str(built)
        if hint.strip() and hint.strip()[:20] not in text:
            res.fail("冒烟", "长度纪律没有进入拼装后的提示词")
        res.note(f"提示词：人设 {len(SYSTEM_PROMPT)} 字，拼装后 {len(text)} 字")
    except Exception as exc:  # noqa: BLE001
        res.fail("冒烟", f"提示词拼装失败: {type(exc).__name__}: {exc}")

    res.note(
        f"关键配置：temperature={cfg.temperature} max_tokens={cfg.max_tokens} "
        f"bubble_max={cfg.bubble_max}"
    )


_CHECKS = [
    ("语法", check_syntax),
    ("导入", check_imports),
    ("冒烟", check_smoke),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check", description="快速自检")
    parser.add_argument("--list", action="store_true", help="只列检查项")
    args = parser.parse_args(argv)

    if args.list:
        for name, _ in _CHECKS:
            print(name)
        return 0

    res = _Result()
    for name, fn in _CHECKS:
        before = len(res.failures)
        try:
            fn(res)
        except Exception as exc:  # noqa: BLE001 — 检查器自己崩了也要报出来
            res.fail(name, f"检查器异常: {type(exc).__name__}: {exc}")
        status = "✓" if len(res.failures) == before else "✗"
        print(f"{status} {name}")

    print()
    for note in res.notes:
        print("  " + note)

    if res.failures:
        print()
        print(f"✗ {len(res.failures)} 项失败：")
        for item in res.failures:
            print("   " + item)
        return 1

    print()
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
