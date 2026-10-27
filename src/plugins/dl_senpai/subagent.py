"""子 agent：把耗时或独立的任务甩到后台跑，主对话不等它。

为什么要有这一层：
主 agent 一轮回复要等它自己的所有工具返回。生图 30~120 秒、找参考图要联网，
串在主链路上等于让对方盯着「正在输入」发呆两分钟。

两类任务共用同一套调度：
1. pipeline（函数型）：给一个 async 函数，跑完把结果交给回调。生图走这类。
2. llm（智能型）：给一个目标和受限工具集，它自己决定调哪个工具、何时收工。
   找参考图走这类——「去哪儿找」这种判断不适合写死成 if/else。

三条硬约束（都是这个项目里踩过的坑）：
- 并发上限：群里连刷几张图时不能同时打上游，否则一起超时、整批都废；
- 超时：作业卡死也要有终点，且回调必须被调用（哪怕 status=failed），
  否则调用方永远等不到结果，表现成「她说画了但永远没有图」；
- 结果只经 on_done 投递：作业自己发消息，不回头找主 agent——那时它早结束这一轮了。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from nonebot import logger

from .observability.logging import log_event

# 作业体：无参 async 函数，返回值就是结果
Factory = Callable[[], Awaitable[Any]]
# 完成回调：可以是同步函数，也可以是 async 函数
OnDone = Callable[["SubAgentResult"], Any]

# 子 agent 每步吐一个 JSON，但模型习惯用三个反引号的 json 代码块包一层，要能剥掉
_FENCE_RE = re.compile(r"\x60{3}(?:json)?\s*(.+?)\x60{3}", re.S)


class SubAgentError(RuntimeError):
    """子 agent 没能按约定收工（超步数、协议不认）。"""


@dataclass
class SubAgentResult:
    """一次子 agent 作业的最终状态。"""

    job_id: str
    name: str
    status: str            # ok / failed / timeout / cancelled
    value: Any = None
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class SubAgentAnswer:
    """智能型子 agent 的收工内容。"""

    answer: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    # 最后一次工具结果：模型收工时经常忘记把 url 写进 payload，
    # 调用方靠这个兜底，不至于前面白搜
    last_observation: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Job:
    job_id: str
    name: str
    factory: Factory
    on_done: OnDone | None
    timeout: float
    started: float = 0.0


class SubAgentRunner:
    """后台作业调度器（进程内单例，见 get_subagent_runner）。"""

    def __init__(self, *, max_concurrent: int = 2, default_timeout: float = 240.0) -> None:
        # 信号量在这里创建、在事件循环里等待：Python 3.10+ 的 Semaphore 不再绑定
        # 创建时的 loop，所以模块导入期建实例也安全
        self._sem = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._jobs: dict[str, _Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._running = 0
        self._peak = 0
        self._total = 0
        self.max_concurrent = max(1, int(max_concurrent))
        self.default_timeout = float(default_timeout)

    # ---------------- 对外 ----------------

    def submit(
        self,
        name: str,
        factory: Factory,
        *,
        timeout: float | None = None,
        on_done: OnDone | None = None,
    ) -> str:
        """派一个后台作业，立刻返回 job_id。

        调用方拿到 id 就该继续干自己的事；结果只会从 on_done 回来。
        """
        job = _Job(
            job_id=uuid4().hex[:12],
            name=name,
            factory=factory,
            on_done=on_done,
            timeout=float(timeout if timeout is not None else self.default_timeout),
        )
        self._jobs[job.job_id] = job
        self._total += 1
        task = asyncio.create_task(
            self._run(job), name=f"dl_senpai_subagent_{name}_{job.job_id}"
        )
        # 必须自己留引用：asyncio 只持弱引用，任务对象被 GC 掉会静默消失
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log_event(
            "subagent.submit",
            name=name,
            job_id=job.job_id,
            timeout=job.timeout,
            pending=len(self._jobs),
        )
        return job.job_id

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "pending": len(self._jobs),
            "peak": self._peak,
            "total": self._total,
            "max_concurrent": self.max_concurrent,
        }

    def pending(self) -> int:
        return len(self._jobs)

    async def drain(self, *, timeout: float = 10.0) -> None:
        """等所有作业结束（测试与优雅退出用）。"""
        if not self._tasks:
            return
        await asyncio.wait(list(self._tasks), timeout=timeout)

    # ---------------- 内部 ----------------

    async def _run(self, job: _Job) -> None:
        job.started = time.monotonic()
        try:
            async with self._sem:
                self._running += 1
                self._peak = max(self._peak, self._running)
                try:
                    value = await asyncio.wait_for(job.factory(), timeout=job.timeout)
                    result = SubAgentResult(job.job_id, job.name, "ok", value=value)
                finally:
                    self._running -= 1
        except asyncio.TimeoutError:
            result = SubAgentResult(
                job.job_id, job.name, "timeout", error=f"超时 {job.timeout:.0f}s"
            )
        except asyncio.CancelledError:
            # 进程退出时取消：记一条日志再往上抛，别把 CancelledError 吃掉
            log_event("subagent.cancelled", name=job.name, job_id=job.job_id)
            self._jobs.pop(job.job_id, None)
            raise
        except Exception as exc:  # noqa: BLE001 — 作业体任何异常都不能冒到调度器外面
            result = SubAgentResult(
                job.job_id, job.name, "failed", error=f"{type(exc).__name__}: {exc}"
            )

        result.elapsed_ms = int((time.monotonic() - job.started) * 1000)
        self._jobs.pop(job.job_id, None)
        log_event(
            "subagent.done" if result.ok else "subagent.failed",
            level="info" if result.ok else "warning",
            name=job.name,
            job_id=job.job_id,
            status=result.status,
            elapsed_ms=result.elapsed_ms,
            error=result.error[:200] or None,
        )
        if job.on_done is None:
            return
        try:
            maybe = job.on_done(result)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception as exc:  # noqa: BLE001 — 回调自己炸了不能影响调度器
            logger.exception(f"dl_senpai subagent on_done 回调失败 name={job.name}: {exc}")


_runner: SubAgentRunner | None = None


def get_subagent_runner(*, max_concurrent: int | None = None) -> SubAgentRunner:
    """取进程内单例；首次调用时的参数决定并发配置。"""
    global _runner
    if _runner is None:
        _runner = SubAgentRunner(
            max_concurrent=max_concurrent if max_concurrent is not None else 2
        )
    return _runner


def reset_subagent_runner() -> None:
    """测试用：丢掉单例，避免上一个用例的并发状态串到下一个。"""
    global _runner
    _runner = None


# ================================================================
# 智能型子 agent：目标 + 受限工具集，自己决定怎么干
# ================================================================

ToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


def _tool_args(data: dict[str, Any]) -> dict[str, Any]:
    """把模型给的参数抠出来。

    为什么要容错：实测 deepseek 会把参数写成 parameters / arguments，
    甚至把 query 直接摊在顶层。只认 args 时工具永远收到空参数，
    然后它就在参数名上反复试错，直到步数耗尽——整个任务白跑。
    """
    for key in ("args", "parameters", "arguments", "params", "input"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {
        k: v for k, v in data.items() if k not in {"action", "tool", "thought", "reason"}
    }


def _tool_name_of(data: dict[str, Any]) -> str:
    """抠出工具名。

    模型对键名的叫法很随意：tool / name / tool_name 都见过，
    还有把工具名直接摊在顶层的。只认 tool 时它会一直「协议违规」，
    白烧掉几步预算。
    """
    for key in ("tool", "name", "tool_name", "toolName"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in data.values():
        if isinstance(value, str) and value.strip() in _TOOLS:
            return value.strip()
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, str) and inner.strip() in _TOOLS:
                    return inner.strip()
    return ""


def _query_of(args: dict[str, Any]) -> str:
    """搜索词也可能叫 q / keywords / search_query，统一认下来。"""
    for key in ("query", "q", "keywords", "keyword", "search_query", "text"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _tool_image_search(args: dict[str, Any]) -> dict[str, Any]:
    """按关键词搜图片，返回直链。复用浏览器那边的 ddgs 实现（不需要 playwright）。"""
    from .browser_agent import _search_images

    query = _query_of(args)
    if not query:
        return {"error": "query 不能为空（参数名用 query）"}
    limit = max(1, min(8, int(args.get("limit") or 3)))
    hits = await _search_images(query, limit=limit)
    return {"query": query, "count": len(hits), "hits": hits}


async def _tool_image_probe(args: dict[str, Any]) -> dict[str, Any]:
    """下载图片并校验能不能用（格式、体积）。下游是图生图接口，喂垃圾会直接 400。"""
    import httpx

    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "url 不合法"}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = resp.content
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not raw:
        return {"ok": False, "error": "空响应"}
    if len(raw) > 4 * 1024 * 1024:
        return {"ok": False, "error": f"太大 {len(raw)} bytes"}
    # 按文件头判断真实格式：很多图床把 webp/jpeg 标成 png，名实不符时上游会拒
    is_image = (
        raw[:3] == b"\xff\xd8\xff"
        or raw[:8] == b"\x89PNG\r\n\x1a\n"
        or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
    )
    return {"ok": is_image, "bytes": len(raw), "url": url}


async def _tool_image_find(args: dict[str, Any]) -> dict[str, Any]:
    """搜索 + 逐张校验，一步返回第一张能用的图。

    为什么把两步合成一个工具：让模型「先搜、再 probe、再决定」要三步，
    实测它会在参数名和顺序上耗光步数。能由代码做的判断，不要交给模型。
    """
    found = await _tool_image_search(args)
    hits = found.get("hits") or []
    candidates: list[dict[str, Any]] = []
    for hit in hits:
        url = str(hit.get("image_url") or "").strip()
        if not url:
            continue
        probe = await _tool_image_probe({"url": url})
        if probe.get("ok"):
            candidates.append(
                {
                    "url": url,
                    "page_url": hit.get("page_url", ""),
                    "title": hit.get("title", ""),
                    "bytes": probe.get("bytes"),
                }
            )
            # 多给几张让模型有得挑：只给一张时它会嫌画风不对、干脆不选
            if len(candidates) >= 3:
                break
    if not candidates:
        return {"error": "没有可用的图片", "tried": len(hits)}
    # url 字段直接放第一张：调用方在模型不肯挑的时候可以直接兜底用它
    return {"url": candidates[0]["url"], "candidates": candidates, "count": len(candidates)}


# 工具说明书：不仅要列名字，还要写清参数长什么样。
# 实测只列名字时模型会说「当前没有可用的 image_find 工具」然后直接收工。
_TOOL_DOCS: dict[str, tuple[str, str]] = {
    "image_find": (
        "搜图 + 自动校验，直接返回可用图片直链（首选，一次就够）",
        '{"query":"凯尔希 官方立绘","limit":3}',
    ),
    "image_search": (
        "只搜不校验，返回图片直链列表",
        '{"query":"凯尔希 官方立绘","limit":3}',
    ),
    "image_probe": ("下载并校验一张图能不能用", '{"url":"https://example.com/a.png"}'),
}


_TOOLS: dict[str, ToolFn] = {
    "image_find": _tool_image_find,
    "image_search": _tool_image_search,
    "image_probe": _tool_image_probe,
}


def register_tool(name: str, fn: ToolFn) -> None:
    """给以后的任务加工具用（新闻检索、比价……）。"""
    _TOOLS[name] = fn


async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """直接调一个工具，不经过模型循环。

    为什么需要：有些活（拼搜索词、下载校验）模型做不好也不该它做。
    调用方可以直接跑工具，只在需要「换策略」时才把活交给模型。
    """
    fn = _TOOLS.get(name)
    if fn is None:
        raise SubAgentError(f"没有这个工具：{name}")
    return await fn(dict(args or {}))


def available_tools() -> list[str]:
    return sorted(_TOOLS)


@dataclass
class SubAgentSpec:
    """一个智能型任务的定义。"""

    name: str
    goal: str
    tools: tuple[str, ...] = ("image_find", "image_search", "image_probe")
    # 3 步够用：一步找图、一步收工，留一步给模型试错
    max_steps: int = 3
    timeout: float = 90.0


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。抠不出来就返回空 dict，由调用方按协议违规处理。"""
    raw = (text or "").strip()
    if not raw:
        return {}
    fence = _FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _chat_json(
    config: Any, messages: list[dict[str, Any]], *, max_tokens: int = 500
) -> dict[str, Any]:
    """单轮对话并解析成 JSON。走主链路的 relay（不是生图那家）。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=config.active_api_key(),
        base_url=config.active_base_url(),
        timeout=60.0,
    )
    resp = await client.chat.completions.create(
        model=config.active_model(),
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.2,
    )
    return _extract_json((resp.choices[0].message.content or ""))


_PERSONA = """你是「{name}」子 agent。你只做一件事：{goal}

可用工具（只能从下面挑，不许自己发明）：
{tool_lines}

规则：
1. 每一步只输出一个 JSON 对象，不要输出别的文字、不要 markdown 代码块。
2. 调工具，参数一律放在 args 里（不要写成 parameters）：
   {{"action":"tool","tool":"image_find","args":{{"query":"凯尔希 立绘","limit":3}}}}
3. 收工：{{"action":"finish","answer":"一句话说明你拿到了什么","payload":{{...}}}}
4. 最多 {max_steps} 步，到点必须 finish；找不到也要 finish 并在 answer 里说清楚。
5. 你是来干活的，不要解释自己、不要客套。"""


async def run_llm_agent(
    config: Any,
    spec: SubAgentSpec,
    *,
    chat: Callable[[Any, list[dict[str, Any]]], Awaitable[dict[str, Any]]] | None = None,
) -> SubAgentAnswer:
    """跑一个智能型任务（同步等待；调用方通常自己在后台作业里）。

    为什么把 chat 做成可注入：测试要能不打真实模型就验证「协议走通 / 超步数收场」。
    """
    chat_fn = chat or _chat_json
    tools = {name: _TOOLS[name] for name in spec.tools if name in _TOOLS}
    doc_lines: list[str] = []
    for name in tools:
        desc, example = _TOOL_DOCS.get(name, ("", "{}"))
        doc_lines.append(f"- {name}：{desc}  示例 args：{example}")
    tool_lines = "\n".join(doc_lines) or "- （无）"
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _PERSONA.format(
                name=spec.name,
                goal=spec.goal,
                tool_lines=tool_lines,
                max_steps=spec.max_steps,
            ),
        },
        {"role": "user", "content": spec.goal},
    ]

    empty_retries = 0
    last_observation: dict[str, Any] = {}
    for step in range(1, max(1, spec.max_steps) + 1):
        data = await chat_fn(config, messages)
        while not data and empty_retries < 2:
            # 模型偶发空回复：补一句催一下，别把这一步算进预算
            empty_retries += 1
            messages.append(
                {"role": "user", "content": '你上一条是空的。只输出一个 JSON 对象。'}
            )
            data = await chat_fn(config, messages)
        action = str(data.get("action") or "").strip().lower()

        if action == "finish":
            payload = data.get("payload")
            return SubAgentAnswer(
                answer=str(data.get("answer") or "").strip(),
                payload=payload if isinstance(payload, dict) else {},
                steps=step,
                last_observation=last_observation,
            )

        if action == "tool":
            tool = _tool_name_of(data)
            args = _tool_args(data)
            fn = tools.get(tool)
            if fn is None:
                observation: Any = {
                    "error": f"没有这个工具：{tool}",
                    "available": sorted(tools),
                }
            else:
                try:
                    observation = await fn(args)
                except Exception as exc:  # noqa: BLE001 — 工具炸了要回灌给模型，别中断
                    observation = {"error": f"{type(exc).__name__}: {exc}"}
            last_observation = observation if isinstance(observation, dict) else {}
            # 把上一轮输出和工具结果都放回上下文：模型要靠它决定下一步
            messages.append(
                {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}
            )
            # 最后一步还在调工具：直接拿这次结果收工。
            # 否则前面辛苦搜到的结果全白费（模型常在这时去「再搜一次」）。
            last_observation = observation if isinstance(observation, dict) else {}
            if step >= max(1, spec.max_steps):
                return SubAgentAnswer(
                    answer=f"最后一步执行了 {tool}",
                    payload=last_observation,
                    steps=step,
                    last_observation=last_observation,
                )
            messages.append(
                {
                    "role": "user",
                    "content": f"工具 {tool} 的结果：\n"
                    + json.dumps(observation, ensure_ascii=False)[:2000],
                }
            )
            continue

        # 协议违规（模型说了人话）：提醒一次，别当场崩
        messages.append(
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}
        )
        messages.append(
            {
                "role": "user",
                "content": '只输出 JSON：{"action":"tool",...} 或 {"action":"finish",...}',
            }
        )

    # 走完预算还没收工：只要中途拿到过工具结果就用它兜底。
    # 实测模型经常在最后一步纠结「再搜一次」，前面搜到的图不该跟着作废。
    if last_observation:
        return SubAgentAnswer(
            answer="超步数收工（用最后一次工具结果）",
            payload=last_observation,
            steps=max(1, spec.max_steps),
            last_observation=last_observation,
        )
    raise SubAgentError(f"子 agent「{spec.name}」在 {spec.max_steps} 步内没按要求收工")
