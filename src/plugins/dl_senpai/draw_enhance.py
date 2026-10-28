"""生图的提示词增强与出图自检。

为什么需要这一层：本中转的 gpt-image-* 实测 revised_prompt 原样返回输入，
**完全不做提示词改写**，也不会回头检查自己画得对不对。ChatGPT 网页版的体验
里这两步都有（内部重写 + 视觉复核），所以在外面自己补上。

三个能力：
1. enhance_prompt —— 把短描述扩写成完整画面描述（构图/镜头/光影/画风）
2. describe_reference —— 看图转述：用户发的参考图变成精确文字，喂给生图模型
3. judge_image —— 生成后看图验收，不合格就把缺陷写成反馈，交给重画

所有函数都是**尽力而为**：任何异常都退回原值，绝不能让增强环节把生图搞挂。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from nonebot import logger
from openai import AsyncOpenAI

from .config import PluginConfig


def _vision_model(config: PluginConfig) -> str:
    return (config.draw_vision_model or "").strip()


def _client(config: PluginConfig) -> AsyncOpenAI:
    """增强走**聊天渠道**的 key。

    为什么不用生图 key：生图分组下只有图像模型，没有能看图的对话模型
    （实测 /v1/models 在生图分组下只返回 gpt-image-* 三个）。
    """
    return AsyncOpenAI(
        api_key=config.active_api_key(),
        base_url=config.active_base_url(),
        timeout=60.0,
    )


def _data_url(raw: bytes, max_side: int = 1024) -> str:
    """把图片压成视觉模型能接的 data URL。

    为什么必须缩：原图动辄 5MB，base64 后更大，直接发会超限或极慢；
    判断「有没有猫耳」「发色对不对」用 1024 长边完全够。
    """
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        im.thumbnail((max_side, max_side))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        raw = buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"dl_senpai draw enhance resize skipped: {exc}")
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


async def _chat(
    config: PluginConfig, content: list[dict[str, Any]], *, max_tokens: int = 500
) -> str:
    model = _vision_model(config)
    if not model:
        return ""
    resp = await _client(config).chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


async def describe_reference(config: PluginConfig, raw: bytes) -> str:
    """看参考图，转成可以直接画出来的描述。"""
    try:
        text = await _chat(
            config,
            [
                {
                    "type": "text",
                    "text": (
                        "这张图会被当作绘图参考。请用中文客观描述：人物外形（发色发型、眼睛、"
                        "族属特征如兽耳兽尾）、服装款式与主色、姿势与构图、背景、画风。"
                        "只输出描述本身，不要评价、不要分点编号，200 字以内。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": _data_url(raw)}},
            ],
            max_tokens=400,
        )
        if text:
            logger.info(f"dl_senpai draw reference described chars={len(text)}")
        return text
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai draw describe_reference failed: {type(exc).__name__}: {exc}")
        return ""


async def enhance_prompt(
    config: PluginConfig,
    *,
    prompt: str,
    user_request: str,
    reference: tuple[str, bytes] | None = None,
    card_spec: str = "",
    locked_entities: tuple[str, ...] = (),
) -> str:
    """把提示词扩写成完整画面描述。

    为什么不直接换掉模型写的那版：扩写只补细节（构图/光影/画风），
    原提示词里的关键特征必须原样保留，所以要求它「改写而不是另起炉灶」。

    为什么还要传 card_spec：实测聊天模型凭记忆写 IP 角色会写错——让它画
    凯尔希，它写出「精灵耳、紫色眼瞳」（实际是菲林族猫耳、绿眼）。角色卡
    虽然会压在最前面纠正，但扩写时若把错误描述原样保留，同一段提示词里
    就出现「绝不是精灵尖耳」和「精灵耳女性角色」自相矛盾，生图模型无从
    取舍。所以这里让官方设定**优先**，草稿与它冲突的部分按设定改写。
    """
    ref_desc = ""
    if reference is not None:
        ref_desc = await describe_reference(config, reference[1])

    instruction = (
        "你是绘图提示词工程师。把下面这段草稿扩写成一段更完整的画面描述，"
        "补上构图、镜头、光影、画风、画质细节。要求："
        "1) 原稿里已有的要素一个都不能丢、不能改；2) 不要新增原稿没有的人物身份或设定；"
        "3) 输出一段连续中文，不要标题、不要编号、不要解释，250 字以内。"
    )
    if card_spec:
        instruction += (
            "\n\n【官方设定，优先级最高】" + card_spec
            + "\n以上官方设定如果和草稿冲突，**按官方设定改写**，并且不要在结果里留下任何"
            "与官方设定矛盾的词（例如官方写「猫耳」而草稿写「精灵耳」，就只保留猫耳）；"
            "官方设定本身也不必逐字重复，融进描述里即可。"
        )
    if locked_entities:
        # 实体锁定：旧版扩写会把「艾雅法拉」同义化成「羊角少女」——读起来通顺，
        # 身份却没了。这里把名字钉进指令，并在返回前做一次硬校验。
        instruction += (
            "\n\n【LOCKED_ENTITY，绝对不可替换】" + " / ".join(locked_entities)
            + "\n这个专有名词必须原样出现在结果里，一个字都不能改，"
            "也不许换成「某位姑娘」「那位少女」这类代称或外形描述。"
        )
    if user_request:
        instruction += "\n\n用户的原话：" + user_request[:200]
    if ref_desc:
        instruction += "\n\n参考图内容（必须与之保持一致）：" + ref_desc
    instruction += "\n\n草稿：" + prompt

    try:
        text = await _chat(config, [{"type": "text", "text": instruction}], max_tokens=600)
        if not text:
            return prompt
        # 兜底：模型偶尔会带上「好的，扩写如下：」这种前缀
        for junk in ("好的，", "扩写如下：", "以下是扩写后的描述：", "画面描述："):
            if text.startswith(junk):
                text = text[len(junk) :].strip()
        if locked_entities:
            # 校验失败就整段弃用，宁可少点润色也不能丢身份
            from .image_intent import validate_entity_preservation

            lost = [
                name for name in locked_entities
                if not validate_entity_preservation(name, (), text)
            ]
            if lost:
                logger.warning(
                    f"dl_senpai draw enhance dropped entity {lost} -> fallback original prompt"
                )
                return prompt
        logger.info(f"dl_senpai draw enhanced prompt {len(prompt)} -> {len(text)} chars")
        return text or prompt
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai draw enhance failed: {type(exc).__name__}: {exc}")
        return prompt


async def judge_image(
    config: PluginConfig,
    image_path: Path,
    *,
    requirements: str,
) -> tuple[bool, str]:
    """验收出图。返回 (是否合格, 不合格的具体问题)。

    为什么不合格要返回问题描述：光重画没用，得把「哪里错了」写进下一次
    提示词，否则同一个模型会以同样的方式再错一遍。
    """
    try:
        raw = Path(image_path).read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai draw judge read failed: {exc}")
        return True, ""

    content = [
        {
            "type": "text",
            "text": (
                "你是校对员。只核对画面是否满足下面【验收要求】里**写明**的项目。\n\n"
                "【验收要求】\n" + requirements[:3000] + "\n\n"
                "【判定规则，必须遵守】\n"
                "1. 只判定【验收要求】里明确写出的项目。要求里没写的特征，一律不算错。\n"
                "2. **不许自己补充角色设定**。你不知道这个角色长什么样，凭印象加进来的要求"
                "会把正确的图判成错的——实测：曾因为「缺少羊耳兽尾」判错一张完全正确的"
                "艾雅法拉，而她的设定里根本没有兽耳兽尾。\n"
                "3. 服装、场景、姿势以「用户要求」为准。用户指定了泳装/便服/浴衣，"
                "缺少默认制服或标志性道具不算错，也不要要求补上。\n"
                "4. 另外看：面部和手部有没有明显崩坏、有没有多余文字。\n\n"
                "只输出两行：第一行「通过」或「不通过」；第二行如果不通过，用一句话说清具体问题"
                "（例如「头发画成了棕色，应为银白偏浅紫」），通过则留空。"
            ),
        },
        {"type": "image_url", "image_url": {"url": _data_url(raw)}},
    ]
    try:
        text = await _chat(config, content, max_tokens=200)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai draw judge failed: {type(exc).__name__}: {exc}")
        return True, ""

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return True, ""
    verdict = lines[0]
    issue = lines[1] if len(lines) > 1 else ""
    passed = "通过" in verdict and "不通过" not in verdict
    logger.info(
        f"dl_senpai draw judge passed={passed} verdict={verdict[:40]!r} issue={issue[:80]!r}"
    )
    return passed, ("" if passed else (issue or "画面与要求不符"))
