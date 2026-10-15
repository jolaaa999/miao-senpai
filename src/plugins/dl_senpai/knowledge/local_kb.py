"""本地 Markdown 知识库检索器。

为什么需要它：
    RAGFlow 那套装的是计算机教材，而且强依赖服务端 —— 服务一挂就什么都查不到。
    有些领域（游戏、学校事务）知识量不大、但更新很勤，适合直接放成本地 md
    文件：零延迟、零网络依赖、改完即生效。

为什么不用向量检索：
    knowledge 包的边界测试明确禁止引入向量库依赖（第五十二节）。
    这里是人手整理的小规模语料，用「词命中 + 覆盖度」打分已经够用，
    上 embedding 只会带来重依赖和不可控的相似度噪声。

设计要点：
    - 一级子目录 = 一个领域；子目录里的 .md = 该领域的知识
    - 跳过 _ 或 . 开头的文件、以及 README.md（给人看的说明，不是知识）
    - 按 Markdown 标题切段，每段一个 chunk
    - 查询词与正文**零重合就直接返回空**：宁可说查不到，
      也不能把不相关段落当"证据"塞给模型 —— 那正是幻觉的来源
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any

from .models import (
    DatasetAccess,
    KnowledgeChunk,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeScope,
    KnowledgeSource,
)

# 这些文件名是给人看的说明或模板，不是知识内容
_SKIP_NAMES = {"readme.md", "index.md"}

# --- 分词 ---
# 拉丁词/数字：连续的字母数字下划线
_ASCII_TERM_RE = re.compile(r"[A-Za-z0-9_]{2,}")
# 中文（含日韩）字符
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")

# 中文用滑动窗口切 n-gram，2 和 3 元同时要：
# 2 元召回高（"太刀"能命中"太刀的配装"），3 元精度高（"怪物猎"没有意义）。
# 只用一种都会偏，所以两种都留，靠长度加权来平衡。
_CJK_NGRAMS = (2, 3)

# 疑问词与口水词：这些词在查询里几乎不携带信息，但会被切成大量无意义 n-gram，
# 把"覆盖率"的分母撑大 —— 实测出现过「社团考核是什么」命中的是「学习方式」
# （撞上"社团"和"什么"），而真正该命中的「考核一」因为只撞上"考核"一个词
# 反而被覆盖率闸门拒掉。所以先剥掉它们，再按"实词"判定。
_STOP_PHRASES = (
    "是什么", "什么是", "为什么", "怎么办", "怎么", "怎样", "如何",
    "哪些", "哪个", "哪里", "什么", "多少", "是否", "请问",
    "帮我", "告诉我", "介绍一下", "一下", "有没有", "能不能",
    "以及", "还有", "吗", "呢", "吧", "呀", "啊", "嘛",
)

# 字段权重：同一个词出现在标题 / 正文 / 文件名里，说明力是不一样的。
# 标题最强（"官方网站"这种标题就是这一段的主题），正文其次，
# 文件名和领域只能算背景提示 —— 给它们同等权重会让"武科大"这种领域词
# 灌进该领域下每一段，把真正的区分度稀释掉（实测会把这问的正确段落挤出前五）。
_FIELD_TITLE = 1.6
_FIELD_BODY = 1.0
_FIELD_CONTEXT = 0.35

# 长度归一化的参数（见 _score_chunk）
#
# 指数为什么是 0.2 这么小：长度惩罚只是为了抵消"长段落词多、撞词机会大"，
# 而这一点现在已经由实词闸门 + IDF 承担了。指数给到 0.4 时实测出现过
#   考核(w=5.46) ÷ 101^0.4   <   社团(w=3.93) ÷ 40^0.4
# —— 1.46 倍的长度惩罚压过了 1.39 倍的 IDF 优势，正确段落被挤下去。
# 0.2 让长度差异退居次要，区分度交还给 IDF。
_LENGTH_EXPONENT = 0.2
# 短段落的下限取平均长度的一半，避免"一行链接"靠极小分母刷分。
# 用相对值而不是固定词数，换了语料也不会失衡。
_LENGTH_FLOOR_RATIO = 0.5

# 打分时对"整词命中"的额外奖励系数
_WHOLE_HIT_BONUS = 2.5
# 领域名/文件名命中的奖励系数
_TITLE_BONUS = 3.0

# 目录扫描结果缓存：(目录快照, 解析出的 chunk 列表)
# 为什么要缓存：预取在每条事实类消息前都会跑，每次重新读盘 + 解析
# 整个知识库没有必要。用 (路径, mtime, 大小) 的快照判断是否要重扫 ——
# 这样"改完即生效"和"不重复读盘"可以同时成立。
_scan_cache: dict[str, tuple[tuple, list[dict], dict[str, int]]] = {}


def _terms(text: str) -> list[str]:
    """把文本切成用于匹配的词（全部小写）。

    中文没有空格，光按空白切会得到一整个句子，什么都匹配不上，
    所以同时产出两类词：拉丁词，以及中文的 2/3 元滑动窗口。
    """
    if not text:
        return []
    low = text.casefold()
    out: list[str] = list(_ASCII_TERM_RE.findall(low))
    for run in _CJK_RE.findall(low):
        if len(run) < 2:
            continue
        # 整段也算一个词：用户很可能直接问一个完整词条名
        out.append(run)
        for n in _CJK_NGRAMS:
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                out.append(run[i:i + n])
    # 去重但保留出现过的所有词；顺序无关紧要
    return list(dict.fromkeys(out))


def _content_terms(text: str) -> list[str]:
    """剥掉疑问词后提取实词。

    为什么不能直接给原查询切词：中文的疑问句尾（"是什么"）会被切成
    一堆无意义 n-gram，既稀释权重又制造假命中。
    剥完若什么都不剩（整句都是口水词），返回空 —— 调用方会退回用全量词。
    """
    cleaned = text or ""
    for phrase in _STOP_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    return _terms(cleaned)


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """拆出 YAML 头与正文。

    只认最简单的 key: value 形式 —— 知识库是人手维护的，
    为了解析头部引入 yaml 依赖不值得，格式写错时退回"整篇都是正文"反而更稳。
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[3:end]
    body = text[end + 4:]
    meta: dict[str, str] = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.lstrip("\n")


# 这些小节名装的是"还没写的东西"，不是知识本身。
# 为什么必须整节排除：我们在待补充清单里写了「校园卡、宿舍、选课等
# 学生事务入口」，结果问「校园卡怎么补办」时这段 TODO 命中了 ——
# 库里根本没有校园卡的内容，却因为这份待办清单被当成了相关资料。
# 把"我还没查"当成"我知道"，这是比不命中更糟的错。
_META_SECTIONS = frozenset({
    "待补充", "TODO", "todo", "待办", "未验证", "前置说明",
    "写知识文件时的注意", "说明", "免责声明",
})


def _split_sections(body: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切段，返回 [(标题, 正文)]。

    为什么按标题切而不是按固定字数切：
    人手整理的 md 本来就是按条目分节的，一节就是一个完整知识点。
    按字数硬切会把条目名和内容拆散，检索质量反而更差。

    名称落在 _META_SECTIONS 里的小节整节丢弃（见该常量的说明）。
    """
    sections: list[tuple[str, str]] = []
    title = ""
    buf: list[str] = []
    skipped = False
    for line in body.splitlines():
        m = re.match(r"^#{1,6}\s+", line)
        if m:
            if buf and not skipped:
                sections.append((title, "\n".join(buf).strip()))
            buf = []
            title = re.sub(r"^#{1,6}\s+", "", line).strip()
            skipped = title in _META_SECTIONS
            continue
        buf.append(line)
    if buf and not skipped:
        sections.append((title, "\n".join(buf).strip()))
    # 丢掉空段（连续标题之间的空档）
    return [(t, c) for t, c in sections if c]


def _snapshot(root: Path) -> tuple:
    """目录快照：文件路径 + mtime + 大小。变了才重扫。"""
    items: list[tuple[str, float, int]] = []
    for path in sorted(root.rglob("*.md")):
        try:
            st = path.stat()
        except OSError:
            continue
        items.append((str(path), st.st_mtime, st.st_size))
    return tuple(items)


def _hay_of(chunk: dict) -> str:
    """一段内容的完整可匹配文本（领域 + 文件名 + 标题 + 正文）。

    为什么把领域和文件名也算进来：它们本身就是主题标签。
    实测「永劫无间 官方」原本只返回一堆泛泛的"官方"段 —— 因为"永劫无间"
    这个词只存在于文件名里，被切分切掉后，正文中没有任何一段能同时命中
    "永劫无间"和"官方"。补上文件上下文后，永劫无间那份文件里的每一段
    才算是真的在讲永劫无间。
    """
    return (
        (chunk.get("domain") or "") + " "
        + (chunk.get("file") or "") + " "
        + (chunk.get("section") or "") + "\n"
        + (chunk.get("text") or "")
    ).casefold()


def _scan(root: Path) -> tuple[list[dict], dict[str, int]]:
    """扫描整个知识库，返回 (chunk 列表, 词频统计 df)。

    为什么要一起返回 df：打分需要 IDF，而 df 必须在全库范围内统计。
    它跟 chunk 列表同属一次扫描的产物，分开缓存只会让两者不同步。
    """
    key = str(root)
    snap = _snapshot(root)
    cached = _scan_cache.get(key)
    if cached is not None and len(cached) == 3 and cached[0] == snap:
        return cached[1], cached[2]

    chunks: list[dict] = []
    for path in sorted(root.rglob("*.md")):
        name = path.name
        if name.startswith("_") or name.startswith("."):
            continue
        if name.casefold() in _SKIP_NAMES:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 单个文件读不了不该拖垮整个检索，跳过即可
            continue
        meta, body = _parse_front_matter(raw)
        # 领域取一级子目录名；文件直接放在根下时用文件名兜底
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = Path(name)
        domain = rel.parts[0] if len(rel.parts) > 1 else ""
        if domain and domain == rel.name:
            domain = ""
        for section, content in _split_sections(body):
            chunks.append({
                "domain": domain,
                "file": name,
                "section": section,
                "text": content,
                "meta": meta,
                "path": path,
                "term_index": None,  # 懒建，见 _term_freq
            })

    # 统计每个词出现在多少个 chunk 里 —— 这是 IDF 的原料。
    # 满大街的词（"内容""说明"）df 高 → 权重低；稀有关键词（"考核"）df 低 → 权重高。
    df: dict[str, int] = {}
    for chunk in chunks:
        terms = _terms(_hay_of(chunk))
        # 词数存回 chunk：长度归一化要用，且这就是扫描的副产品，不必二次计算
        chunk["_term_count"] = len(terms)
        for term in set(terms):
            df[term] = df.get(term, 0) + 1

    _scan_cache[key] = (snap, chunks, df)
    return chunks, df


def reset_local_kb_cache() -> None:
    """清空扫描缓存（测试与热重载用）。"""
    _scan_cache.clear()


def _term_freq(terms: list[str]) -> dict[str, int]:
    """统计一段文本里各词出现次数。"""
    freq: dict[str, int] = {}
    for t in terms:
        freq[t] = freq.get(t, 0) + 1
    return freq


def _topic_terms(domain: str) -> set[str]:
    """领域名 + 领域自称别名切出来的词 —— 只说明"属于哪个领域"，说明不了答案。

    为什么不用"在领域内出现频率过半"来判定（我第一版就是这么写的）：
    武科大目录下只有两个文件，ACM俱乐部.md 的名字里带 acm/俱乐部，
    切出来的词超过了半数，于是"acm 俱乐部"被误判成领域通用词 ——
    结果问「ACM俱乐部怎么加入」时，唯一实质词被抹掉，正确答案反而被拒。

    注意**不把文件名算进来** —— 文件名是主题而不是容器，
    「ACM俱乐部.md」这个名字本身就是对「ACM俱乐部怎么加入」的有效命中。
    """
    return set(_terms(domain or ""))


def _score_chunk(
    chunk: dict,
    query_terms: list[str],
    query_text: str,
    core_terms: list[str] | None = None,
    df: dict[str, int] | None = None,
    total_chunks: int = 0,
    avg_len: float = 0.0,
    domain_terms: set[str] | None = None,
) -> float:
    """给一个 chunk 打分。

    分数 = 命中词的加权和 / 长度惩罚。

    为什么要除以长度的平方根：
    长段落天然更容易命中词，不加惩罚的话永远是大段内容排前面，
    短小精悍的条目反而被淹没。

    闸门为什么从"覆盖率比例"改成"必须命中实词"：
    比例闸门对中文不成立 —— 一个长查询里大部分词位是疑问词切出来的碎片，
    分母被撑大后，只命中核心词的**正确段落**反而会被拒。
    现在改成：查询里只要有实词，就必须命中至少一个，否则判为不相关。
    """
    hay = _hay_of(chunk)
    if not hay:
        return 0.0
    hay_terms = _terms(hay)
    if not hay_terms:
        return 0.0

    freq = _term_freq(hay_terms)

    def _idf(term: str) -> float:
        """罕见词权重高。没传 df 时退化为 1（不改变原有相对顺序）。"""
        if not df or total_chunks <= 0:
            return 1.0
        d = df.get(term, 0) or 1
        return math.log(1.0 + total_chunks / d)

    # 只用实词计分：疑问词切出来的碎片不该进入分数，
    # 否则"什么"这种碎片会让无关段落靠撞词挤上来。
    scoring_terms = [t for t in (core_terms or []) if t] or list(query_terms)

    matched = [t for t in scoring_terms if freq.get(t)]
    if not matched:
        return 0.0

    section_low = (chunk.get("section") or "").casefold()
    text_low = (chunk.get("text") or "").casefold()

    def _field_weight(term: str) -> float:
        if term in section_low:
            return _FIELD_TITLE
        if term in text_low:
            return _FIELD_BODY
        return _FIELD_CONTEXT

    # 去掉被更长的命中词包含的短词。
    # 为什么必须去重：中文 n-gram 是重叠切出来的，一个"永劫无间"会同时产生
    # 永劫/劫无/无间/永劫无/劫无间 共 6 个命中 —— 不去重等于把同一个词算 6 遍，
    # 结果是一段只是反复提到该词、却答非所问的内容压过正确答案。
    maximal = [t for t in matched if not any(t != o and t in o for o in matched)]

    hits = len(maximal)
    score = 0.0
    for t in maximal:
        n = freq.get(t, 0)
        # 词越长越具体、越罕见越有区分度；出现多次有额外收益但迅速饱和
        score += (
            len(t) * _idf(t) * _field_weight(t)
            * (1.0 + min(n - 1, 3) * 0.3)
        )

    # 实词闸门：只有命中实词才算相关。
    # 实词为空（整句都是疑问词）时退回"有任何命中即可"。
    core = [t for t in (core_terms or []) if t]
    if core:
        core_hits = [t for t in set(core) if t in freq]
        if not core_hits:
            return 0.0
        # 闸门收紧：查询里"非主题词"的部分必须至少有一个命中。
        # 只命中主题词（武科大、怪猎这种领域名）说明不了任何问题 ——
        # 武科大目录下**每一段**都含"武科大"，于是问宿舍、问食堂、问图书馆
        # 都能拿到一团噪音，而每一段其实都没回答问题。
        topical = domain_terms or set()
        substantive = [t for t in core if t not in topical]
        if substantive:
            if not any(t in freq for t in set(substantive)):
                return 0.0
        # 命中实词越多，说明越贴题
        coverage = len(core_hits) / len(set(core))
    else:
        coverage = hits / max(1, len(set(query_terms)))

    # 标题命中的加成：**只认整句命中**，不再"命中任意一个词就翻倍"。
    # 为什么改：标题本来就已算进 hay（见 _hay_of），词命中已被计分；
    # 原来再乘一个 2.5 倍，会让一个烂大街的词（"网站"）只要出现在标题里
    # 就压过在正文里命中稀有关键词（"刷题"）的段落 —— 实测
    # 「刷题网站是什么」因此返回了永劫无间的官网，而正确的那段进不去。
    if query_text and len(query_text) >= 2 and query_text.casefold() in section_low:
        # 整句就是这一段的标题 —— 这种命中几乎可以确定就是用户要的
        score *= _TITLE_BONUS

    floor = (avg_len or 0.0) * _LENGTH_FLOOR_RATIO
    eff_len = max(len(hay_terms), floor, 1.0)
    return score * (0.5 + coverage) / (eff_len ** _LENGTH_EXPONENT)


class LocalMarkdownRetriever:
    """把本地 knowledge_base 目录当作知识来源检索。

    Args:
        root: 知识库根目录。不存在时视为"空库"，不报错。
        name: provider 名（观测用）。
        min_score: 低于这个分数的命中直接丢弃。默认 0 表示"有命中就留"，
            实际的过滤主要靠覆盖度闸门。
    """

    def __init__(
        self,
        root: Any = "knowledge_base",
        *,
        name: str = "local_md",
        min_score: float = 0.0,
    ) -> None:
        self._root = Path(root)
        self._name = name
        self._min_score = float(min_score)

    @property
    def name(self) -> str:
        return self._name

    @property
    def root(self) -> Path:
        return self._root

    def list_sources(self) -> list[KnowledgeSource]:
        """每个领域一个 source，方便上层展示与权限登记。"""
        if not self._root.exists():
            return []
        out: list[KnowledgeSource] = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            out.append(KnowledgeSource(
                source_id="local:" + child.name,
                name="本地知识库 · " + child.name,
                scope=KnowledgeScope.GLOBAL,
                access=DatasetAccess.PUBLIC,
                description="本地 Markdown 知识库",
                metadata={"path": str(child)},
            ))
        return out

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        """执行检索。**约定不抛异常**，失败时返回 ok=False。"""
        started = time.perf_counter()

        def _elapsed() -> float:
            return (time.perf_counter() - started) * 1000.0

        if not self._root.exists():
            return KnowledgeResult(
                chunks=[], provider=self._name, duration_ms=_elapsed(),
                ok=True, metadata={"note": "目录不存在，视为空库"},
            )

        text = (query.query or "").strip()
        if not text:
            return KnowledgeResult(
                chunks=[], provider=self._name, duration_ms=_elapsed(), ok=True
            )

        try:
            all_chunks, df = _scan(self._root)
            _lens = [c.get("_term_count") or 0 for c in all_chunks]
            avg_len = (sum(_lens) / len(_lens)) if _lens else 0.0
        except Exception as exc:  # noqa: BLE001 — 扫盘失败不能让整条回复挂掉
            return KnowledgeResult(
                chunks=[], provider=self._name, duration_ms=_elapsed(),
                ok=False, error=type(exc).__name__,
            )

        if not all_chunks:
            return KnowledgeResult(
                chunks=[], provider=self._name, duration_ms=_elapsed(), ok=True
            )

        query_terms = _terms(text)
        # 实词：剥掉疑问词后剩下的。为空时退回全量词（见 _score_chunk 的闸门）
        core_terms = _content_terms(text)
        if not query_terms:
            return KnowledgeResult(
                chunks=[], provider=self._name, duration_ms=_elapsed(), ok=True
            )

        # 查询点名了某个领域时，只在那个领域里找。
        #
        # 为什么必须有这一步：问「永劫无间官网」时，"永劫无间"是主题词被排除，
        # 只剩"官网"当实质词 —— 而"官网"几乎每个文件都有，
        # 于是武科大那批长文件全部涌上来，真正该命中的永劫无间条目被挤掉。
        # 主题词被排除出**计分**是对的（它区分不了段与段），
        # 但它完全可以用来**限定检索范围** —— 那正是它最擅长的。
        _core_set = set(t for t in (core_terms or []) if t)
        named_domains: set[str] = set()
        for chunk in all_chunks:
            dom = chunk.get("domain") or ""
            if dom and (_topic_terms(dom) & _core_set):
                named_domains.add(dom)
        if named_domains:
            all_chunks = [
                c for c in all_chunks if (c.get("domain") or "") in named_domains
            ]
            # 领域变了，长度基准也要跟着重算，否则惩罚会按旧的均值算
            _lens = [c.get("_term_count") or 0 for c in all_chunks]
            avg_len = (sum(_lens) / len(_lens)) if _lens else 0.0
            if not all_chunks:
                return KnowledgeResult(
                    chunks=[], provider=self._name, duration_ms=_elapsed(),
                    ok=True, metadata={"matched": 0},
                )

        scored: list[tuple[float, dict]] = []
        for chunk in all_chunks:
            s = _score_chunk(
                chunk, query_terms, text, core_terms, df, len(all_chunks), avg_len,
                _topic_terms(chunk.get("domain") or ""),
            )
            if s > self._min_score:
                scored.append((s, chunk))
        if not scored:
            return KnowledgeResult(
                chunks=[], provider=self._name, duration_ms=_elapsed(),
                ok=True, metadata={"matched": 0},
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[: max(1, int(query.top_k))]

        # 归一化到 0~1，纯粹为了日志好看，不影响排序
        best = top[0][0] or 1.0
        out: list[KnowledgeChunk] = []
        for score, chunk in top:
            domain = chunk.get("domain") or ""
            label = (domain + " / " + chunk["file"]) if domain else chunk["file"]
            meta = dict(chunk.get("meta") or {})
            meta["domain"] = domain
            out.append(KnowledgeChunk(
                text=chunk["text"],
                chunk_id="%s#%s" % (chunk["file"], chunk.get("section") or ""),
                score=round(score / best, 4),
                source_id="local:" + domain if domain else "local",
                source_name=label,
                section=chunk.get("section") or "",
                uri=str(chunk.get("path") or ""),
                metadata=meta,
            ))

        return KnowledgeResult(
            chunks=out, provider=self._name, duration_ms=_elapsed(), ok=True,
            metadata={"scanned": len(all_chunks), "matched": len(scored)},
        )


__all__ = [
    "LocalMarkdownRetriever",
    "reset_local_kb_cache",
]
