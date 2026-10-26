"""技能层（Skills）：给模型**按需**塞一页说明书。

定位（明确写死，防止以后被误用）：
    技能 = 一份 Markdown 说明书，说明「这类任务该怎么做」。
    技能 **不** 执行任何东西、**不** 注册工具、**不** 编排多步流程。
    真需要执行的任务归 agent loop + ToolRegistry；
    技能只负责把「专家经验」在合适的时候放进提示里。
    这条边界是有意的：一旦技能能执行，它就会和工具层抢职责，
    调 试 成本立刻翻倍。

为什么要有这一层（痛点）：
    人设提示词是**全局**的，写不出「做 PPT 时先定大纲、配色只用两色系」这种
    领域规范——写进去会污染闲聊，不写又每次都靠临场发挥。
    技能层解决的就是「领域规范只在该出现的那一回合出现」。

渐进披露（省钱的关键）：
    目录扫描只解析 frontmatter（name / description / triggers），
    命中之后才读正文；没命中就完全不进提示。
    所以「装了 20 个技能」≠「每条消息多 20 页上下文」。

存储格式：data/skills/<name>/SKILL.md，开头是可选的 frontmatter：
    ---
    name: ppt-design
    description: 做 PPT 的版式与配色规范
    triggers: ppt, 幻灯片, 演示文稿
    ---
    正文……

    刻意不用 YAML 库：解析失败要能"降级成不加载"而不是抛异常，
    而且这里只有三个标量字段，多一个依赖不值得。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .observability.logging import log_event

# 单条技能正文注入上限的兜底值（配置里可改）。
# 1500 字大约是「一页纸的要点」，再长就稀释注意力、也开始抢人设的戏份。
DEFAULT_MAX_CHARS = 1500
# 单条消息最多注入几个技能。默认 1：两个领域规范同时上，模型会互相打架。
DEFAULT_MAX_SKILLS = 1

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class SkillSpec:
    """一条技能的元信息（正文懒加载，见 `SkillRegistry.body_of`）。"""

    name: str
    description: str
    triggers: tuple[str, ...]
    path: Path

    def matches(self, text: str) -> int:
        """命中的触发词个数；0 表示不命中。

        用「命中个数」而不是「命中/不命中」：同时提到 ppt 和 幻灯片 的消息
        显然比只提到一个的更确定要这个技能，排序靠它。
        """
        if not self.triggers:
            # 没有 triggers 的技能永远不自动注入 —— 否则任何一条消息都会命中，
            # 等于把技能变成常驻提示词。想常驻就写进人设，别写技能。
            return 0
        low = text.lower()
        return sum(1 for t in self.triggers if t and t.lower() in low)


@dataclass
class SkillRegistry:
    """一个目录下的技能集合。启动时扫一次，正文按需读。"""

    root: Path
    _specs: list[SkillSpec] = field(default_factory=list)
    _loaded: bool = False
    # 上次加载时的文件指纹，用于热重载判断（见 maybe_reload）
    _signature: tuple[tuple[str, int], ...] = ()

    def load(self, *, force: bool = False) -> list[SkillSpec]:
        """扫描目录。解析失败的单个技能被跳过（不连坐其他技能）。"""
        if self._loaded and not force:
            return self._specs
        specs: list[SkillSpec] = []
        if self.root.is_dir():
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir():
                    continue
                doc = entry / "SKILL.md"
                if not doc.is_file():
                    continue
                spec = _parse_skill(entry.name, doc)
                if spec is not None:
                    specs.append(spec)
        self._specs = specs
        self._loaded = True
        self._signature = self.signature()
        return specs

    def all(self) -> list[SkillSpec]:
        return self.load()

    def signature(self) -> tuple[tuple[str, int], ...]:
        """目录里所有 SKILL.md 的 (路径, mtime_ns) 指纹。

        为什么用 mtime 而不是文件内容哈希：技能文档是**手写小文件**，
        每次改都必然刷新 mtime；哈希要把所有正文读一遍，
        等于把懒加载的意义丢掉（每条消息读 N 份文档）。
        """
        if not self.root.is_dir():
            return ()
        sig: list[tuple[str, int]] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            doc = entry / "SKILL.md"
            try:
                sig.append((str(doc), doc.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(sig)

    def maybe_reload(self) -> bool:
        """指纹变了就重扫，返回是否真的重扫过。

        解决的实际问题：改完 SKILL.md 要重启整个 bot 才生效，
        而调提示词本来就是"改一句、问一句"的活，重启一次 10 秒起步。
        """
        if not self._loaded:
            self.load()
            return True
        if self.signature() == self._signature:
            return False
        self.load(force=True)
        return True

    def match(self, text: str, *, limit: int = DEFAULT_MAX_SKILLS) -> list[SkillSpec]:
        """按触发词命中数排序，取前 `limit` 个。"""
        scored = [(spec.matches(text), spec) for spec in self.load()]
        hits = [(score, spec) for score, spec in scored if score > 0]
        # 命中数相同时按名字排，保证同样的消息每次注入同样的技能（可回归）。
        hits.sort(key=lambda item: (-item[0], item[1].name))
        return [spec for _, spec in hits[: max(0, limit)]]

    def body_of(self, spec: SkillSpec) -> str:
        """读正文。读不到就返回空串，调用方按"没命中"处理。"""
        try:
            return spec.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


_REGISTRIES: dict[str, SkillRegistry] = {}


def get_skill_registry(root: str | Path) -> SkillRegistry:
    """按目录缓存 registry：同一目录只扫一次盘。

    测试里改目录不会串味（不同 path → 不同实例），
    需要重扫时调用方传 `force=True` 走 `load()`。
    """
    key = str(Path(root))
    registry = _REGISTRIES.get(key)
    if registry is None:
        registry = SkillRegistry(root=Path(root))
        _REGISTRIES[key] = registry
    return registry


def reset_skill_registry() -> None:
    """清空缓存（测试与"运行中改了技能文档"时用）。"""
    _REGISTRIES.clear()


def _parse_skill(dir_name: str, path: Path) -> SkillSpec | None:
    """解析一份 SKILL.md；返回 None 表示这份不可用（记一条日志，不抛）。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log_event("skill.unreadable", name=dir_name, error=type(exc).__name__)
        return None
    meta: dict[str, str] = {}
    body = raw
    matched = _FRONTMATTER_RE.match(raw)
    if matched:
        body = raw[matched.end():]
        for line in matched.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    if not body.strip():
        log_event("skill.empty", name=dir_name)
        return None
    triggers = tuple(
        t.strip() for t in meta.get("triggers", "").split(",") if t.strip()
    )
    return SkillSpec(
        name=meta.get("name") or dir_name,
        description=meta.get("description", ""),
        triggers=triggers,
        path=path,
    )


def catalog_text(registry: SkillRegistry) -> str:
    """技能清单（`名字：说明` 每行一条）。

    这里**只提供文本、不自动注入**：现在只有一两个技能，把清单塞进每轮提示是纯开销；
    它的用途是控制台/排障时"到底装了哪些技能、触发词是什么"一眼可见。
    等技能多到模型会漏判时，再把这份清单接进提示（渐进披露第一层）。
    """
    lines: list[str] = []
    for spec in registry.all():
        triggers = "/".join(spec.triggers) or "（无触发词，不会自动注入）"
        lines.append(f"{spec.name}：{spec.description or '（无说明）'} [触发: {triggers}]")
    return "\n".join(lines)


def build_skill_brief(
    text: str,
    config: Any,
    *,
    registry: SkillRegistry | None = None,
) -> str:
    """把命中的技能拼成一段可直接塞进 user_payload 的说明。

    返回空串表示"这轮不需要技能"，调用方据此判断要不要动 payload。

    为什么拼进 user_payload 而不是系统提示词：
        系统提示词是全局人格，为它新增插槽要改 persona/registry（还有字节级等价测试守着）；
        而知识库结果、联网结果、链接正文**都是**拼进 user_payload 的 ——
        技能和它们是同一类东西（这一轮的临时材料），跟着同一个惯例走，
        改动面最小，也不会让技能内容污染人格定义。
    """
    try:
        reg = registry or get_skill_registry(getattr(config, "skill_dir", "data/skills"))
        limit = int(getattr(config, "skill_max_skills", DEFAULT_MAX_SKILLS) or 0)
        if limit <= 0:
            return ""
        if bool(getattr(config, "skill_hot_reload", True)):
            reg.maybe_reload()
        hits = reg.match(text, limit=limit)
        if not hits:
            return ""
        budget = int(getattr(config, "skill_max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
        blocks: list[str] = []
        for spec in hits:
            body = _clip(reg.body_of(spec).strip(), budget)
            if not body:
                continue
            blocks.append(f"【技能：{spec.name}】\n{body}")
            log_event("skill.injected", name=spec.name, chars=len(body))
        if not blocks:
            return ""
        header = "（以下是与本次请求相关的做法说明，按它来做；与人格冲突时以人格为准）"
        return header + "\n\n" + "\n\n".join(blocks)
    except Exception as exc:  # noqa: BLE001 — 技能加载失败绝不能挡住回复
        log_event("skill.failed", error=f"{type(exc).__name__}: {exc}")
        return ""


def _clip(body: str, budget: int) -> str:
    """按字数截断。技能文档是**步骤说明**，该保的是开头（结论与顺序）。"""
    if budget <= 0 or len(body) <= budget:
        return body
    return body[:budget].rstrip() + "\n（正文过长，已截断）"
