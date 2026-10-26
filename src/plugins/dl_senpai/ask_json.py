"""向模型要一行 JSON 的内部判断（刷帖挑帖、回留言都用它）。

为什么单独一个模块
------------------
这条路径有几个反直觉的坑，踩一次就够了：

1. **必须走裸接口**，不能走 llm.chat()。后者会叠人设、清洗、简短化、
   接地检查，结果把"只输出一行 JSON"改写成一篇带小标题的回答（实测踩过）。
2. **指令必须放在 user 里**。中转的 system 角色会被它自己的开场白盖掉：
   给了 system 也照样回"你没说要做什么"（实测）。
3. 失败时要能退回人格链路，但不能因此把整轮活动搞崩。

内部判断不需要她的人格，只需要规整的输出——这就是这个模块存在的理由。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ask_json"]


async def ask_json(cfg: Any, instruction: str, data: str, *, llm: Any = None) -> str:
    """让模型按 instruction 只回一行 JSON。返回原始文本（调用方自己解析）。"""
    if llm is None:
        try:
            import httpx

            url = str(cfg.active_base_url()).rstrip("/") + "/chat/completions"
            payload = {
                "model": str(cfg.active_model()),
                "messages": [
                    # 指令和资料都塞 user：见模块顶部第 2 条
                    {"role": "user", "content": instruction + "\n\n" + data},
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            }
            headers = {
                "Authorization": f"Bearer {cfg.active_api_key()}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
            return str(body["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001 — 裸接口不通就退回人格链路，别让整轮崩掉
            pass
    from .llm import get_llm

    client = llm if llm is not None else get_llm(cfg)
    result = await client.chat(
        [], instruction + "\n\n" + data, sender_name="自己", system_prompt_override=""
    )
    return (getattr(result, "text", "") or "").strip()


def first_json(text: str) -> dict[str, Any]:
    """从模型回复里抠第一段 JSON。抠不出就当空。"""
    import json

    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
