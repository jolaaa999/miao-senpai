"""跨场合对话流：把她在各处说过的话合成一条时间线。

为什么单独一层（来自 ai-companion 包的 memory.js）：
    ChatMemory 按**会话**存：群 A 一本、群 B 一本、私聊一本。
    于是同一个人换个地方，她就失忆——「我刚在私聊跟你说的那事」
    在群里提起来，她一无所知。这不是记忆不够长，是**账本分错了维度**。

    所以这一层按**时间**记，不分场合，只记「谁、在哪、说了什么、她怎么回的」。

与现有两层的关系：
    ChatMemory   她参与的对话，进 messages（按会话）
    RecentChat   本会话当天全部流水（含别人闲聊），进 system prompt
    CrossChat    跨会话的「她参与过的轮次」时间线，进 system prompt
    三者不重复：RecentChat 记录的是**发生过的消息**，这一层记录的是
    **她实际接过的对话**，且跨场合。

隐私边界（比原实现收紧的一处）：
    原包不区分场合，全部渲染，群里能看见私聊内容。
    这里定了条硬规则：**私聊内容只允许出现在私聊**。
    群里只注入「其它群」的片段，私聊里可以看见群里的公开内容。
    泄漏一次比「记住」重要得多。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any

# 单人/单会话之外的全局流水默认留多少条（跨场合，所以不能太大）
DEFAULT_MAX_ENTRIES = 600
# 渲染时单条正文上限，防止一条长回复把整段预算吃光
_MAX_TEXT = 160


def _default_dir() -> Path:
    return Path("data") / "dl_senpai" / "cross_chat"


class CrossChatStore:
    """append-only 的 JSONL 流水。

    用 JSONL 而不是 JsonStore（整文件重写）：
    这里每天几百条追加，重写整个文件是纯浪费；追加失败也不该影响主流程。
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.dir = Path(directory) if directory else _default_dir()
        self.path = self.dir / "turns.jsonl"
        self._lock = Lock()

    def record(
        self,
        *,
        where: str,
        who: str,
        text: str,
        reply: str,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        """记一轮。写失败直接吞掉——记流水不能挡住回复。"""
        row = {
            "t": time.time(),
            "where": str(where or ""),
            "who": str(who or "")[:30],
            "text": " ".join(str(text or "").split())[:_MAX_TEXT],
            "reply": " ".join(str(reply or "").split())[:_MAX_TEXT],
        }
        if not row["text"] and not row["reply"]:
            return
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + chr(10))
                # 超量裁剪：只在小概率时机做，避免每次都读全文
                if max_entries > 0 and int(row["t"]) % 20 == 0:
                    self._trim(max_entries)
        except Exception:  # noqa: BLE001
            pass

    def _trim(self, max_entries: int) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= max_entries:
                return
            self.path.write_text(
                chr(10).join(lines[-max_entries:]) + chr(10), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    def turns(self, n: int = 40) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-max(n * 4, n):]:
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(row, dict):
                out.append(row)
        return out[-n:]


_store: CrossChatStore | None = None
_store_lock = Lock()


def get_cross_store(directory: str | None = None) -> CrossChatStore:
    """取全局单例。目录可配，测试可传临时目录。"""
    global _store
    with _store_lock:
        if _store is None or (directory and str(_store.dir) != str(Path(directory))):
            _store = CrossChatStore(directory)
        return _store


def _visible(row: dict[str, Any], current_where: str) -> bool:
    """场合可见性。私聊内容只许出现在私聊。"""
    where = str(row.get("where") or "")
    if not where or where == current_where:
        # 同场合的不注入：那部分 ChatMemory 已经喂给她了，重复只会挤预算
        return False
    if where.startswith("private:"):
        return current_where.startswith("private:")
    return True


def build_cross_brief(
    *,
    current_where: str,
    n: int = 8,
    max_chars: int = 800,
    directory: str | None = None,
    include_reply: bool = False,
) -> str:
    """渲染「刚才别处发生的事」。空则返回空串，调用方据此决定不注入。

    默认**不渲染她自己的回复**（include_reply=False）。

    为什么要改：原来每条都带上「你：<她当时的原话>」，等于在提示词里塞了 8 条
    她自己的旧输出当范文。实测那段里全是判词腔（「这一刀就变得很致命：绩点是
    软排序，德育线是硬门槛」）、自我点评（「「17」是什么的 17」）、以及「洛希
    极限这句话没有前文」这类元话语——正是要消灭的毛病。而模型看到自己的旧
    输出会当成风格样板继续模仿，越聊越像机器。
    「记住别处聊过什么」需要的是**对方的原话**，不是她自己怎么回的。
    """
    if n <= 0 or max_chars <= 0:
        return ""
    store = get_cross_store(directory)
    rows = [r for r in store.turns(n * 3) if _visible(r, current_where)][-n:]
    if not rows:
        return ""

    lines: list[str] = ["【刚才别处的事】（她是当事人，别装作不知道，也别生硬复述）"]
    for row in rows:
        when = time.strftime("%H:%M", time.localtime(float(row.get("t") or 0)))
        place = "群里" if str(row.get("where", "")).startswith("group:") else "私聊"
        who = str(row.get("who") or "某人")
        text = str(row.get("text") or "").strip()
        reply = str(row.get("reply") or "").strip()
        if text:
            lines.append(f"  {when} {who}（{place}）：{text}")
        if reply and include_reply:
            lines.append(f"       你：{reply}")
    text = chr(10).join(lines)
    return text[:max_chars]
