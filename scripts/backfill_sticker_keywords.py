"""给表情包库回填「含义」——一次性维护脚本。

为什么要做这件事：
    data/dl_senpai/stickers/*.json 里 145 条记录的 context_text 和 keywords
    **全是空的**，而 stickers.score_sticker 恰恰只靠这两个字段打分。
    后果是任何 STICKER 查询都只拿到「类型加分」，排序等同随机——
    工具跑得通，却永远挑不出「合适的那张」。

    采集当时的聊天上下文已经丢了（context_text 是空串，补不回来），
    所以含义只能从图片本身重新理解：用视觉模型逐张看图，
    产出「一句话描述 + 情绪词」写回 keywords。
    这样模型写 STICKER 时才能真正命中一张贴切的图。

用法：
     python scripts/backfill_sticker_keywords.py --limit 5     # 先试 5 张
     python scripts/backfill_sticker_keywords.py             # 全量（去重后约 106 张）
     python scripts/backfill_sticker_keywords.py --apply-only  # 只用缓存写回，不调模型

     结果缓存在 data/dl_senpai/stickers/.vision_cache.json，
     可随时 Ctrl-C 中断，重跑会跳过已完成的。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_STICKER_DIR = _ROOT / "data" / "dl_senpai" / "stickers"
_CACHE_PATH = _STICKER_DIR / ".vision_cache.json"

# 提示词刻意压成「只输出一行」：deepseek-v4.1-flash 是推理模型，
# 放开写它会把思考过程一起吐出来，反而不利于解析。
_PROMPT = (
    "看这张聊天表情包。只输出一行，不要任何解释、不要标题、不要换行。\n"
    "格式严格为：画面描述|情绪词1,情绪词2,情绪词3\n"
    "画面描述：不超过 20 字，说清楚画的是什么、什么表情。\n"
    "情绪词：3-5 个中文词，覆盖这张图适合用在什么场合"
    "（如 开心/委屈/生气/无语/震惊/害羞/加油/晚安）。"
)

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".gif": "image/gif", ".webp": "image/webp"}


def _load_env() -> dict[str, str]:
    """读 .env 与 .env.dev，后者覆盖前者（与 config.py 的 env_file 顺序一致）。"""
    merged: dict[str, str] = {}
    for name in (".env", ".env.dev"):
        path = _ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            merged[key.strip()] = value.strip().strip('"').strip("'")
    return merged


def _iter_index_files() -> list[Path]:
    return sorted(p for p in _STICKER_DIR.glob("*.json") if p.is_file())


def _records(index_path: Path) -> list[dict]:
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    for key in ("records", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


# 这些词出现在「描述」或「情绪词」里，说明那是模型的思考过程而不是答案。
# deepseek-v4.1-flash 是推理模型，经常在真正的答案后面再补一段自我检查，
# 而那段里往往带着它正在照抄的模板占位符（情绪词1、画面描述 之类）。
_META_HINTS = (
    "情绪词1", "情绪词2", "情绪词3", "输出", "换行", "格式", "要求",
    "确保", "严格", "不要", "标题", "we need", "the image", "no title",
    "answer only", "一行", "emotion words", "maybe", "but need",
    "since form", "final output", "使用", "need ensure", "format:",
    "no extra", "extra spaces",
)

# 行首的自我检查前缀，剥掉之后剩下的才是真描述。
_PREFIX_RE = re.compile(r"^(最终一行|最终|这行符合|检查|但|格式|输出|必须|需要)\s*[:：]?\s*")

# 模型常把真答案写在「最终：」后面，而自我检查写在前面。
# 取最后一个「最终：」之后的片段，往往正好是干净的那半句。
_TAIL_MARKERS = ("最终输出：", "最终一行：", "最终：")


def _looks_meta(desc: str, words: list[str]) -> bool:
    blob = (desc + " " + " ".join(words)).lower()
    return any(hint in blob for hint in _META_HINTS)


def _parse_answer(raw: str):
    """从模型输出里抠出「描述|词,词」。

    取**最后一个通过检查**的候选行：推理模型会把答案写在中间、
    把自我检查写在最后，所以简单取最后一行经常会抓到思考过程。
    """
    best = None
    for line in (raw or "").splitlines():
        # chr(96) 是反引号：模型常把答案包在 markdown 代码标记里，顺手剥掉。
        line = line.strip().replace(chr(96), "").strip("*# ").strip()
        if "|" not in line:
            continue
        desc, _, words = line.partition("|")
        desc = re.sub(r"^(画面描述|描述)\s*[:：]\s*", "", desc).strip()
        desc = _PREFIX_RE.sub("", desc).strip()
        # 真答案常跟在「最终：」后面，前半截是模型的自我检查
        for marker in _TAIL_MARKERS:
            if marker in desc:
                desc = desc.rsplit(marker, 1)[-1].strip()
        word_list = [w.strip() for w in re.split(r"[,，、/]", words) if w.strip()]
        # 占位符不是情绪词，混进来会让这张图永远匹配不上真实情绪
        word_list = [w for w in word_list if len(w) <= 8 and "情绪词" not in w][:6]
        if not desc or not word_list:
            continue
        if _looks_meta(desc, word_list):
            continue
        best = (desc[:60], word_list)
    return best


def _describe(local_path: Path, env: dict[str, str], timeout: float = 90.0):
    mime = _MIME.get(local_path.suffix.lower())
    if mime is None:
        return None
    try:
        b64 = base64.b64encode(local_path.read_bytes()).decode()
    except OSError:
        return None
    base = env.get("OPENAI_BASE_URL", "").rstrip("/")
    key = env.get("OPENAI_API_KEY", "")
    model = env.get("OPENAI_MODEL", "deepseek-v4.1-flash")
    if not base or not key:
        raise SystemExit("缺少 OPENAI_BASE_URL / OPENAI_API_KEY，无法调用视觉模型")
    payload = {
        "model": model,
        "temperature": 0,
        # 给足 token：推理模型的思考也算在里面，压太小真正的答案会被截断。
        "max_tokens": 900,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}},
        ]}],
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    return _parse_answer(body["choices"][0]["message"].get("content") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少张（0=全部）")
    ap.add_argument("--apply-only", action="store_true", help="不调模型，只用缓存写回")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    ap.add_argument("--sleep", type=float, default=0.3, help="每次调用后的间隔秒数")
    args = ap.parse_args()

    cache: dict[str, dict] = {}
    if _CACHE_PATH.is_file():
        cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    env = _load_env()

    # 去重：同一张图可能出现在多个群的索引里，只需解读一次。
    pending: dict[str, Path] = {}
    for index in _iter_index_files():
        for rec in _records(index):
            rid = str(rec.get("id") or "")
            if not rid or rid in cache or rec.get("keywords"):
                continue
            local = str(rec.get("local_path") or "")
            candidate = _STICKER_DIR / "files" / str((rec.get("segment_data") or {}).get("file") or "")
            # files/ 是权威副本；local_path 指向别处时只在它确实存在才用。
            path = Path(local) if local and Path(local).is_file() else candidate
            if path.is_file():
                pending[rid] = path

    print("待解读 " + str(len(pending)) + " 张（已有缓存 " + str(len(cache)) + " 张）")
    if args.limit:
        pending = dict(list(pending.items())[: args.limit])

    if not args.apply_only:
        done = 0
        for rid, path in pending.items():
            ok = None
            for attempt in (1, 2):
                try:
                    ok = _describe(path, env)
                    if ok:
                        break
                except Exception as exc:  # noqa: BLE001
                    print("  [" + rid + "] 第" + str(attempt) + "次失败: "
                          + type(exc).__name__ + " " + str(exc)[:120])
                    time.sleep(1.5)
            if ok:
                cache[rid] = {"desc": ok[0], "words": ok[1], "file": path.name}
                done += 1
                print("  [" + str(done) + "/" + str(len(pending)) + "] " + rid + " "
                      + path.name + " -> " + ok[0] + " | " + ",".join(ok[1]))
            else:
                print("  [" + rid + "] 放弃（解析不出或调用失败）")
            time.sleep(args.sleep)
            # 每张就存一次盘：中断后不用重来。
            _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.dry_run:
        print("dry-run：不写回索引")
        return 0

    # 写回：keywords = 情绪词 + 整句描述（score_sticker 对 keywords 与
    # context_text 都做子串匹配，放整句能让「委屈」这类查询也命中）。
    touched = 0
    for index in _iter_index_files():
        records = _records(index)
        changed = False
        for rec in records:
            entry = cache.get(str(rec.get("id") or ""))
            if not entry:
                continue
            merged = list(dict.fromkeys([*entry["words"], entry["desc"]]))
            if rec.get("keywords") != merged:
                rec["keywords"] = merged
                changed = True
        if changed:
            index.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            touched += 1
    print("写回完成：更新了 " + str(touched) + " 个索引文件，缓存共 " + str(len(cache)) + " 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
