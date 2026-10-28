"""人设卡命令行：不进群也能管理。

用法（在仓库根目录跑）：

    python scripts\persona_card.py list                 列出所有卡
    python scripts\persona_card.py check                 逐张体检
    python scripts\persona_card.py bind group:105 某卡   绑定
    python scripts\persona_card.py unbind group:105      取消绑定
    python scripts\persona_card.py export 某卡          把人设导出成一张卡

为什么要有这个：群里 `/人设` 只管当前会话，而「这个群该用哪张卡」经常是
一次性配置；export 则是为了「先复制一份现有人设、再在副本上改」——直接
改 persona.py 会污染所有人的她。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 与其它 scripts 一致：直接跑脚本时也能 import 到插件包
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins"))

from dl_senpai.config import get_config  # noqa: E402
from dl_senpai.personas import body_hash, get_persona_store  # noqa: E402


def _store():
    return get_persona_store(get_config())


def cmd_list() -> int:
    store = _store()
    cards = store.cards()
    print(f"目录：{store.directory}")
    if not cards:
        print("（空。放一个 md 进去就能用，模板见 README.md）")
        return 0
    bindings = store.bindings()
    for card in cards:
        used = sorted(k for k, v in bindings.items() if v == card.id)
        mark = "!" if card.problems else " "
        # 来源标记：= 与 persona.py 一致；≠ 已漂移；空 表示不是导出的副本
        synced = card.source_synced
        origin = "" if synced is None else ("=" if synced else "≠")
        print(
            f"{mark}{origin:2} {card.id:16} {card.name:10} {len(card.body):5d} 字  "
            f"{','.join(used) or '-'}"
        )
        if synced is False:
            print("       ↑ 和 persona.py 不一致：要么这份被改过，要么 persona.py 后来改过。"
                  "想以这份为准就删掉 source_hash 行；想重新同步就 export --force。")
        if card.description:
            print(f"    {card.description}")
    print(f"默认卡：{store.default_id or '（无，未绑定的会话继续用 persona.py）'}")
    return 0


def cmd_check() -> int:
    store = _store()
    bad = 0
    for card in store.cards():
        stale = card.source_synced is False
        if not card.problems and not stale:
            print(f"OK   {card.id}")
            continue
        bad += 1
        print(f"WARN {card.id}")
        for problem in card.problems:
            print(f"     - {problem}")
        if stale:
            print("     - 与 persona.py 不同步（两份文案并存，改哪份算数得先定）")
    if not store.cards():
        print("（没有卡可检查）")
    return 1 if bad else 0


def cmd_bind(session_key: str, card_id: str) -> int:
    card = _store().bind(session_key, card_id)
    if card is None:
        print(f"没有「{card_id}」这张卡")
        return 1
    print(f"{session_key} -> {card.id}（{card.name}）")
    return 0


def cmd_unbind(session_key: str) -> int:
    removed = _store().unbind(session_key)
    print("已取消" if removed else "本来就没绑")
    return 0 if removed else 1


def cmd_export(card_id: str, *, force: bool = False) -> int:
    """把人设导出成一张新卡。

    用途：想在「现在的她」基础上改一版给某个群。导出的是**当前** persona.py
    里的正文，不是某张卡——这样导出来的每一版都能追溯到线上正在跑的那份。
    """
    from dl_senpai.persona import SYSTEM_PROMPT

    store = _store()
    if store.get(card_id) is not None and not force:
        print(f"「{card_id}」已经存在；确实要覆盖就加 --force")
        return 1
    directory = Path(store.directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{card_id}.md"
    body = SYSTEM_PROMPT.strip()
    path.write_text(
        "---\n"
        f"id: {card_id}\n"
        f"name: {card_id}\n"
        "# groups: 1050332675\n"
        "description: 从 persona.py 导出的副本\n"
        "source_ref: persona.py\n"
        # 记下导出时的指纹：之后谁改了哪一份都能看出来，不用人肉 diff 三千字
        f"source_hash: {body_hash(body)}\n"
        "---\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {path}")
    print("提醒：改这张卡不会影响没绑定它的会话；两份文本并存时要记得改哪份算数。")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    action, rest = argv[0], argv[1:]
    if action == "list":
        return cmd_list()
    if action == "check":
        return cmd_check()
    if action == "bind" and len(rest) == 2:
        return cmd_bind(rest[0], rest[1])
    if action == "unbind" and len(rest) == 1:
        return cmd_unbind(rest[0])
    if action == "export" and len(rest) == 1:
        return cmd_export(rest[0])
    if action == "export" and len(rest) == 2 and rest[1] in ("--force", "-f"):
        return cmd_export(rest[0], force=True)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
