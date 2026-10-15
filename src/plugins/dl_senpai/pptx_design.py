"""PPT 渲染管线：模型只给结构化内容，排版全部由这里决定。

这段代码的设计依据来自对成熟商业方案（Kimi Slides / 咨询级 deck）的
逆向分析，核心结论有三条：

1. **产出必须是原生可编辑 PPTX**，不能用整页截图贴片。
   截图方案一旦交付，用户在 Office 里一个字都改不了 —— 对课件、
   汇报这类「拿到就要改」的场景等于废品。

2. **模型只给结构化内容，坐标系由代码持有。**
   把 add_textbox(x, y, w, h) 这类原语交给模型是通病，模型对二维
   空间没有稳定直觉，出来的东西必然错位压字。所以版式查表、坐标
   写死在这里。

3. **页面的「像样」来自固定骨架，而不是配色。**
   对标商业咨询 deck 的实际版式，每个内容页都有同一套骨架：
       深色标题带（承载断言式标题）
       → 章节定位条（章节码 + 竖线 + 小节名）
       → 高密度正文（框架图 / 多栏证据 / 细线分割）
       → 底部收束条（一句话 so-what）
       → 左下脚注 + 右下页码
   骨架统一，整份文档才像「同一个人做的」。

配套的排版禁令（照抄成熟规范，避免一股 AI 味）：
    - 不默认用圆角卡片分组（信息容器用细线、留白、字号层级）
    - 不用阴影、发光、3D、蓝紫渐变、霓虹
    - 图标一律单色线稿或编号标记，不混用彩色 emoji
    - 图表背景透明，数据直接标注而不是全靠图例
    - 正文区不留整块空白 —— 空一片是「粗糙」最严重的形式

主题不是「一组调色板」，而是一套**语义令牌**：
    bg        页面底色
    title     标题文字色（在 bg 上）
    fg        正文色（在 bg 上）
    fg_muted  次要文字色
    panel     浅色底板块（解释栏、收束条、信息块）
    line      细分隔线
    accent    点缀色（短线、编号、强调、结论）
    band      顶部色条 / 图表主色
    hero_bg   整版色块页的底色（标题带、章节页、整页配图色带）
    hero_fg   hero_bg 上的文字色
有了这层语义，「深色主题」才成立 —— 否则 primary 既要当深色底
又要当浅色字，必然自相矛盾。

字体只挑 Windows 自带的中文字体（雅黑/等线/宋体/楷体/黑体），
否则对方机器上会回退成宋体，整份观感立刻垮掉。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 幻灯片尺寸：16:9 宽屏（英寸）
W = 13.333
H = 7.5

# ---- 页面骨架（单位英寸，改这里就能整体调版式）----
BAND_H = 1.24          # 顶部标题带高度
NAV_TOP = 1.38         # 章节定位条
NAV_H = 0.42
BODY_TOP = 1.94        # 正文区顶部
BODY_LEFT = 0.78       # 左右留白
BODY_WIDTH = 11.77
BODY_BOTTOM = 6.46     # 无收束条时的正文底
BODY_BOTTOM_S = 6.06   # 有收束条时的正文底
TAKE_TOP = 6.10        # 收束条位置
TAKE_H = 0.58
FOOT_TOP = 6.88        # 脚注 / 页码行

_WHITE = (0xFF, 0xFF, 0xFF)


# ----------------------------------------------------------------
# 主题：语义令牌 + 字体搭配
# ----------------------------------------------------------------

THEMES: dict[str, dict[str, Any]] = {
    "academic": {
        "label": "学术靛蓝",
        "use": "课程、复习、论文讲解",
        "font_title": "微软雅黑",
        "font_body": "微软雅黑",
        "bg": _WHITE,
        "title": (0x25, 0x36, 0x5B),
        "fg": (0x33, 0x33, 0x33),
        "fg_muted": (0x8C, 0x8C, 0x8C),
        "panel": (0xF2, 0xF4, 0xF8),
        "line": (0xD9, 0xDE, 0xE7),
        "accent": (0xE0, 0x6C, 0x4F),
        "band": (0x25, 0x36, 0x5B),
        "hero_bg": (0x25, 0x36, 0x5B),
        "hero_fg": _WHITE,
    },
    "business": {
        "label": "商务石墨",
        "use": "汇报、方案、项目总结",
        "font_title": "微软雅黑",
        "font_body": "微软雅黑",
        "bg": _WHITE,
        "title": (0x2F, 0x34, 0x37),
        "fg": (0x33, 0x33, 0x33),
        "fg_muted": (0x8A, 0x8A, 0x8A),
        "panel": (0xF4, 0xF5, 0xF7),
        "line": (0xDC, 0xDE, 0xE1),
        "accent": (0x3D, 0x7E, 0xAA),
        "band": (0x2F, 0x34, 0x37),
        "hero_bg": (0x2F, 0x34, 0x37),
        "hero_fg": _WHITE,
    },
    "warm": {
        "label": "暖阳橙",
        "use": "活动介绍、轻松话题、社团分享",
        "font_title": "微软雅黑",
        "font_body": "微软雅黑",
        "bg": _WHITE,
        "title": (0x7A, 0x3E, 0x2E),
        "fg": (0x3B, 0x30, 0x2A),
        "fg_muted": (0x9B, 0x8C, 0x80),
        "panel": (0xFB, 0xF3, 0xE7),
        "line": (0xE8, 0xDA, 0xC6),
        "accent": (0xE9, 0xA2, 0x3B),
        "band": (0x7A, 0x3E, 0x2E),
        "hero_bg": (0x7A, 0x3E, 0x2E),
        "hero_fg": _WHITE,
    },
    "forest": {
        "label": "森林绿",
        "use": "生物、环境、农业、医药",
        "font_title": "微软雅黑",
        "font_body": "微软雅黑",
        "bg": _WHITE,
        "title": (0x1E, 0x4D, 0x3A),
        "fg": (0x2E, 0x3A, 0x33),
        "fg_muted": (0x8A, 0x9B, 0x90),
        "panel": (0xEF, 0xF5, 0xF0),
        "line": (0xD3, 0xE2, 0xD7),
        "accent": (0x7C, 0xB3, 0x42),
        "band": (0x1E, 0x4D, 0x3A),
        "hero_bg": (0x1E, 0x4D, 0x3A),
        "hero_fg": _WHITE,
    },
    "ocean": {
        "label": "深海青",
        "use": "科技、工程、算法、实验报告",
        "font_title": "等线",
        "font_body": "等线",
        "bg": _WHITE,
        "title": (0x0F, 0x3B, 0x52),
        "fg": (0x2B, 0x38, 0x40),
        "fg_muted": (0x84, 0x94, 0x9E),
        "panel": (0xEC, 0xF4, 0xF8),
        "line": (0xCF, 0xE0, 0xE9),
        "accent": (0x2E, 0xA8, 0xC4),
        "band": (0x0F, 0x3B, 0x52),
        "hero_bg": (0x0F, 0x3B, 0x52),
        "hero_fg": _WHITE,
    },
    "ink": {
        "label": "水墨朱砂",
        "use": "语文、历史、人文、思政",
        "font_title": "宋体",
        "font_body": "微软雅黑",
        "bg": (0xFD, 0xFC, 0xF9),
        "title": (0x1C, 0x1B, 0x19),
        "fg": (0x33, 0x31, 0x2E),
        "fg_muted": (0x94, 0x8E, 0x86),
        "panel": (0xF4, 0xF1, 0xEA),
        "line": (0xE0, 0xDA, 0xCE),
        "accent": (0xB2, 0x3A, 0x2E),
        "band": (0x1C, 0x1B, 0x19),
        "hero_bg": (0x1C, 0x1B, 0x19),
        "hero_fg": (0xF7, 0xF4, 0xEE),
    },
    "sakura": {
        "label": "樱粉",
        "use": "文艺、生活、心理健康、招新",
        "font_title": "楷体",
        "font_body": "微软雅黑",
        "bg": (0xFF, 0xFB, 0xFC),
        "title": (0x8E, 0x3B, 0x5A),
        "fg": (0x3F, 0x33, 0x37),
        "fg_muted": (0xA8, 0x93, 0x9A),
        "panel": (0xFC, 0xF0, 0xF3),
        "line": (0xF0, 0xD8, 0xDE),
        "accent": (0xE0, 0x8A, 0xA4),
        "band": (0x8E, 0x3B, 0x5A),
        "hero_bg": (0x8E, 0x3B, 0x5A),
        "hero_fg": _WHITE,
    },
    "midnight": {
        "label": "午夜蓝（深色）",
        "use": "技术分享、发布会、晚间讲座",
        "font_title": "微软雅黑",
        "font_body": "微软雅黑",
        "bg": (0x14, 0x18, 0x26),
        "title": (0xF2, 0xF5, 0xFA),
        "fg": (0xD6, 0xDC, 0xE6),
        "fg_muted": (0x8A, 0x94, 0xA8),
        "panel": (0x1E, 0x24, 0x36),
        "line": (0x32, 0x3A, 0x52),
        "accent": (0x4F, 0xA8, 0xE0),
        "band": (0x0B, 0x0E, 0x18),
        "hero_bg": (0x0B, 0x0E, 0x18),
        "hero_fg": (0xF2, 0xF5, 0xFA),
    },
    "carbon": {
        "label": "碳黑（深色）",
        "use": "设计、产品、创意、作品集",
        "font_title": "微软雅黑",
        "font_body": "微软雅黑",
        "bg": (0x17, 0x17, 0x19),
        "title": (0xF5, 0xF5, 0xF7),
        "fg": (0xD8, 0xD8, 0xDC),
        "fg_muted": (0x8C, 0x8C, 0x92),
        "panel": (0x23, 0x23, 0x26),
        "line": (0x38, 0x38, 0x3D),
        "accent": (0xF2, 0x8B, 0x30),
        "band": (0x0E, 0x0E, 0x10),
        "hero_bg": (0x0E, 0x0E, 0x10),
        "hero_fg": (0xF5, 0xF5, 0xF7),
    },
}

DEFAULT_THEME = "academic"


# ----------------------------------------------------------------
# 底层绘制 helper
# ----------------------------------------------------------------


def _rgb(triple):
    from pptx.dml.color import RGBColor

    return RGBColor(*triple)


def _blend(a, b, alpha):
    """把 a 向 b 混合 alpha（0-1），用于把照片压暗。"""
    return tuple(int(round(a[i] * (1 - alpha) + b[i] * alpha)) for i in range(3))


def _font(run, *, size, theme, color_key="fg", bold=False, italic=False, title=False):
    """设置字体。

    中文必须同时写 a:ea 槽，否则 PowerPoint 会回退成宋体，
    整套配色再好看也会立刻显旧 —— 这一步最容易漏。
    """
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    name = (theme.get("font_title") if title else theme.get("font_body")) or "微软雅黑"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = name
    run.font.color.rgb = _rgb(theme.get(color_key) or theme["fg"])
    try:
        rpr = run._r.get_or_add_rPr()
        ea = rpr.find(qn("a:ea"))
        if ea is None:
            ea = rpr.makeelement(qn("a:ea"), {})
            rpr.append(ea)
        ea.set("typeface", name)
    except Exception:  # noqa: BLE001
        pass


def _rect(slide, *, left, top, width, height, color, line=None, line_w=0.75):
    """画一个直角矩形。

    一律直角：成熟规范里明确禁止把圆角卡片当默认分组容器，
    圆角 + 阴影是「AI 味」最明显的来源。
    """
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.shadow.inherit = False
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = _rgb(line)
        shape.line.width = Pt(line_w)
    return shape


def _hairline(slide, left, top, width, color, thickness=0.014):
    """细分割线。整份文档的「结构感」主要靠这些线，而不是靠色块。"""
    return _rect(slide, left=left, top=top, width=width, height=thickness, color=color)


def _vline(slide, left, top, height, color, thickness=0.014):
    return _rect(slide, left=left, top=top, width=thickness, height=height, color=color)


def _circle(slide, *, cx, cy, r, fill, line=None):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.OVAL, Inches(cx - r), Inches(cy - r), Inches(r * 2), Inches(r * 2)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill)
    shape.shadow.inherit = False
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = _rgb(line)
    return shape


def _chevron(slide, *, left, top, width, height, color):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.CHEVRON, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _textbox(slide, *, left, top, width, height):
    from pptx.util import Inches

    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    return frame


def _align(para, which: str):
    """设置段落对齐。

    python-pptx 的 PP_ALIGN 是 LEFT=1 / CENTER=2 / RIGHT=3，
    手写数字当参数等于把居中写成左对齐，而且不会报错，
    只能靠肉眼看出来 —— 所以一律用常量。
    """
    from pptx.enum.text import PP_ALIGN

    para.alignment = PP_ALIGN.CENTER if which == "center" else PP_ALIGN.RIGHT


def _valign(shape, which: str = "middle"):
    from pptx.enum.text import MSO_ANCHOR

    shape.text_frame.vertical_anchor = (
        MSO_ANCHOR.MIDDLE if which == "middle" else MSO_ANCHOR.TOP
    )


def _para(frame, first: bool):
    return frame.paragraphs[0] if first else frame.add_paragraph()


def _line(frame, text, *, size, theme, color_key="fg", bold=False, first=False,
          space_after=6, title=False, align=None, line_spacing=None):
    """写一行文本，返回段落。"""
    from pptx.util import Pt

    para = _para(frame, first)
    run = para.add_run()
    run.text = text
    _font(run, size=size, theme=theme, color_key=color_key, bold=bold, title=title)
    para.space_after = Pt(space_after)
    if line_spacing:
        para.line_spacing = line_spacing
    if align:
        _align(para, align)
    return para


# ----------------------------------------------------------------
# 文本度量：估算用（只用于选字号与判断是否溢出）
# ----------------------------------------------------------------


def _per_line(size: float, width_in: float) -> int:
    """估算一行放得下多少个全角字符。中文一字约等于一个字号宽。"""
    return max(6, int(width_in * 72 / size))


def _text_height(text: str, size: float, width_in: float, space_after: float = 0) -> float:
    chars = _per_line(size, width_in)
    lines = max(1, -(-len(text) // chars))
    return (lines * size * 1.34 + space_after) / 72


def _bullets_of(raw: Any, limit: int = 14) -> list[tuple[str, int]]:
    """规范化要点，并识别层级。

    层级不新增参数：以「- 」「· 」或缩进开头即视为二级要点，
    模型按常见写法缩进就行，不必学新格式。
    """
    out: list[tuple[str, int]] = []
    for item in list(raw or [])[:limit]:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            level = 1 if item.get("level") else 0
        else:
            text = str(item or "").strip()
            level = 0
            if text.startswith(("  ", "\t")) or text[:2] in ("- ", "· ", "– "):
                level = 1
                text = text.lstrip("\t -·–").strip()
            elif text.startswith("• "):
                text = text[2:].strip()
        if text:
            out.append((text[:160], level))
    return out


def _bullets_size(bullets, width_in: float) -> float:
    total = sum(len(t) for t, _ in bullets)
    longest = max((len(t) for t, _ in bullets), default=0)
    if total > 300 or longest > 44:
        return 14.0
    if total > 200 or longest > 34:
        return 16.0
    return 18.0


def _grow_size(bullets, width_in, avail_in, *, big=11, small=7, cap=24.0):
    """在可用高度内尽量放大字号。

    字号偏小 + 下半页空一大块，是「粗糙」最直观的表现。
    所以先按内容量取一个基准字号，再逐档放大到接近填满可用高度为止。
    """
    base = _bullets_size(bullets, width_in)
    best = base
    for step in (base, base + 2, base + 4, base + 6, base + 8):
        if step > cap:
            break
        height = sum(
            _text_height(t, step if not lv else max(11.0, step - 2), width_in,
                         big if not lv else small)
            for t, lv in bullets
        )
        if height <= avail_in * 0.94:
            best = step
        else:
            break
    return min(cap, best)


def _block_top(avail_in, block_h, base_top):
    """内容整体竖直居中：底部空一块比上下均分更显得没做完。"""
    return base_top + max(0.0, (avail_in - block_h) / 2)


def _blank(prs, theme):
    """新建空白页并铺上主题底色。

    深色主题全靠这一步：python-pptx 默认白底，
    不显式设置的话，深色主题会变成「深色文字块浮在白纸上」。
    """
    from pptx.dml.color import RGBColor

    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 6 = 空白版式
    try:
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor(*theme["bg"])
    except Exception:  # noqa: BLE001
        pass
    return slide


# ----------------------------------------------------------------
# 配图：裁切、压暗、贴图
# ----------------------------------------------------------------


def _prepare_crop(src, dst, width_in, height_in):
    """把图片按目标框裁切成正好填满的尺寸。

    直接 add_picture 指定宽高会把图**拉伸变形** —— 封面图一变形
    整份 deck 立刻掉档。所以先用 ImageOps.fit 做「等比缩放 + 居中裁切」。

    centering 取 0.42 而非 0.5：人像和主体通常偏上，
    正中间裁常常把头顶切掉。
    """
    try:
        from PIL import Image, ImageOps

        img = Image.open(src).convert("RGB")
        target = (max(64, int(width_in * 150)), max(64, int(height_in * 150)))
        fitted = ImageOps.fit(img, target, method=Image.LANCZOS, centering=(0.5, 0.42))
        fitted.save(str(dst), "JPEG", quality=88)
        return Path(dst) if Path(dst).is_file() else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ppt crop failed: " + str(exc))
        return None


def _band_photo(src, dst, width_in, height_in, alpha=0.66):
    """裁切并压暗，用作标题带底图。

    压暗在 PIL 里做而不是用 PowerPoint 的半透明蒙版：python-pptx 设
    alpha 要直接改 XML，容易踩坑；而且压暗后文字对比度是确定的，
    不会因为在别的渲染器里透明度失效而变成「白字压白图」。
    """
    try:
        from PIL import Image, ImageOps

        img = Image.open(src).convert("RGB")
        target = (max(64, int(width_in * 150)), max(64, int(height_in * 150)))
        fitted = ImageOps.fit(img, target, method=Image.LANCZOS, centering=(0.5, 0.35))
        dark = Image.new("RGB", fitted.size, (12, 14, 20))
        fitted = Image.blend(fitted, dark, alpha)
        fitted.save(str(dst), "JPEG", quality=88)
        return Path(dst) if Path(dst).is_file() else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ppt band photo failed: " + str(exc))
        return None


def _picture(slide, src, *, left, top, width, height, mode="crop"):
    """把图片贴到指定框内；失败返回 False，调用方降级为纯色版式。"""
    from pptx.util import Inches

    if not src:
        return False
    src_path = Path(src)
    if not src_path.is_file():
        return False
    # 裁切结果按「源文件 + 目标尺寸 + 模式」缓存，同一张图重复排版不用重算
    suffix = "" if mode == "crop" else "_dark"
    cache = src_path.with_name(
        src_path.stem + suffix + "_" + str(int(width * 100)) + "x"
        + str(int(height * 100)) + ".jpg"
    )
    if not cache.is_file():
        maker = _prepare_crop if mode == "crop" else _band_photo
        if maker(src_path, cache, width, height) is None:
            return False
    try:
        slide.shapes.add_picture(str(cache), Inches(left), Inches(top),
                                 Inches(width), Inches(height))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("ppt picture failed: " + str(exc))
        return False


def _img_for(ctx, key):
    if not key or not ctx:
        return None
    return (ctx.get("images") or {}).get(str(key).strip())


# ----------------------------------------------------------------
# 页面骨架：标题带 + 章节定位条 + 收束条 + 脚注
# ----------------------------------------------------------------


def _band(prs, theme, ctx, *, image_key=None, solid_color=None):
    """顶部标题带：一张压暗照片，或纯主题色。

    照片带是商业 deck 的签名元素，但每页配一张图要几十秒生成，
    所以默认走纯色带 —— 靠骨架统一来撑观感，而不是靠堆图。
    """
    slide = _blank(prs, theme)
    color = solid_color or theme["hero_bg"]
    art = _img_for(ctx, image_key)
    painted = False
    if art:
        painted = _picture(slide, art, left=0, top=0, width=W, height=BAND_H, mode="dark")
    if not painted:
        _rect(slide, left=0, top=0, width=W, height=BAND_H, color=color)
    # 左侧竖向强调块：给标题带一个明确的起点，视线有落点
    _rect(slide, left=0, top=0, width=0.16, height=BAND_H, color=theme["accent"])
    return slide


def _band_title(slide, title, theme, *, size=27):
    """标题带里的断言式标题（白字）。

    标题必须是**完整结论句**而不是话题词 —— 只读标题就能听出整个
    故事线，这是判断结构成不成立的硬标准。
    """
    frame = _textbox(slide, left=BODY_LEFT + 0.12, top=0.26, width=BODY_WIDTH - 1.0,
                     height=BAND_H - 0.44)
    frame.vertical_anchor = None
    para = _line(frame, title, size=size, theme=theme, color_key="hero_fg",
                 bold=True, first=True, space_after=0, title=True)
    para.line_spacing = 1.12
    return frame


def _nav_bar(slide, spec, theme, ctx):
    """章节定位条：章节码 + 竖线 + 小节名（左），本页作用说明（右）。

    任何一页都要能回答「我在整份文档的哪个位置」，
    这是商业 deck 和信息堆砌最直观的区别。
    """
    chapter = str(spec.get("chapter") or "").strip()
    section = str(spec.get("section") or "").strip()
    scope = str(spec.get("scope") or "").strip()
    x = BODY_LEFT
    if chapter:
        cf = _textbox(slide, left=x, top=NAV_TOP - 0.05, width=1.0, height=NAV_H)
        _line(cf, chapter, size=19, theme=theme, color_key="title", bold=True,
              first=True, space_after=0, title=True)
        x += 0.62
    if section:
        _vline(slide, x, NAV_TOP + 0.03, 0.3, theme["line"], 0.016)
        x += 0.16
        sf = _textbox(slide, left=x, top=NAV_TOP - 0.02, width=6.4, height=NAV_H)
        _line(sf, section, size=13, theme=theme, color_key="fg", bold=True,
              first=True, space_after=0)
    if scope:
        qf = _textbox(slide, left=W - BODY_LEFT - 5.6, top=NAV_TOP + 0.01,
                      width=5.6, height=NAV_H)
        _line(qf, scope, size=11, theme=theme, color_key="fg_muted", first=True,
              space_after=0, align="right")
    _hairline(slide, BODY_LEFT, BODY_TOP - 0.14, BODY_WIDTH, theme["line"])


def _takeaway_bar(slide, spec, theme):
    """底部收束条：一句话 so-what。

    把上面的证据收成一条可执行结论 —— 没有这一条，页面就只是
    「罗列了一堆东西」，看完不知道要干什么。
    """
    text = str(spec.get("takeaway") or "").strip()
    if not text:
        return None
    _rect(slide, left=BODY_LEFT, top=TAKE_TOP, width=BODY_WIDTH, height=TAKE_H,
          color=theme["panel"])
    _rect(slide, left=BODY_LEFT, top=TAKE_TOP, width=0.055, height=TAKE_H,
          color=theme["accent"])
    frame = _textbox(slide, left=BODY_LEFT + 0.26, top=TAKE_TOP + 0.13,
                     width=BODY_WIDTH - 0.5, height=TAKE_H - 0.24)
    _line(frame, text, size=13, theme=theme, color_key="fg", first=True,
          space_after=0, bold=True)
    return frame


def _footnote(slide, spec, theme, page, deck_footer=""):
    """左下脚注 + 右下页码。

    规范里这两项是固定的，既不省略也不当装饰用。
    """
    note = str(spec.get("footer") or deck_footer or "").strip()
    if note:
        nf = _textbox(slide, left=BODY_LEFT, top=FOOT_TOP, width=9.4, height=0.34)
        _line(nf, note, size=9, theme=theme, color_key="fg_muted", first=True,
              space_after=0)
    pf = _textbox(slide, left=W - BODY_LEFT - 1.0, top=FOOT_TOP, width=1.0, height=0.34)
    _line(pf, str(page), size=10, theme=theme, color_key="fg_muted", first=True,
          space_after=0, align="right")


def _content_frame(prs, spec, theme, page, ctx, deck, *, title_size=27):
    """所有内容页共用的骨架，返回 (slide, 正文底边)。"""
    slide = _band(prs, theme, ctx, image_key=spec.get("headerImage"))
    _band_title(slide, str(spec.get("title") or "").strip(), theme, size=title_size)
    _nav_bar(slide, spec, theme, ctx)
    _takeaway_bar(slide, spec, theme)
    _footnote(slide, spec, theme, page, deck.get("footer") or "")
    return slide, (BODY_BOTTOM_S if str(spec.get("takeaway") or "").strip() else BODY_BOTTOM)


def _marker(slide, *, cx, cy, r, text, theme, filled=True):
    """编号圆点标记。

    规范要求「直接标注」而不是靠图例 —— 编号标记是最稳的一种，
    不需要任何图标字体或位图资源。
    """
    _circle(slide, cx=cx, cy=cy, r=r,
            fill=theme["accent"] if filled else theme["panel"])
    frame = _textbox(slide, left=cx - r, top=cy - r + 0.015, width=r * 2, height=r * 2)
    para = _line(frame, str(text), size=11, theme=theme,
                 color_key="hero_fg" if filled else "title", bold=True,
                 first=True, space_after=0, align="center")
    para.line_spacing = 1.0
    return frame


# ----------------------------------------------------------------
# 封面 / 章节页
# ----------------------------------------------------------------


def _cover(prs, deck: dict, theme: dict, ctx=None):
    """封面：整幅配图 + 深色压暗 + 标题块。

    配图拿不到时降级为「左侧色块 + 右侧浅底」的纯色封面，
    不会出现半张图或空白。
    """
    slide = _blank(prs, theme)
    art = _img_for(ctx, deck.get("coverImage"))
    if art and _picture(slide, art, left=0, top=0, width=W, height=H, mode="dark"):
        # 压暗后的整幅图当底：文字区再加一层实色，保证对比度确定
        _rect(slide, left=0, top=H - 3.05, width=W, height=3.05, color=theme["hero_bg"])
        _rect(slide, left=0, top=0, width=0.2, height=H, color=theme["accent"])
        text_left, text_w = 1.1, 11.2
    else:
        # 没有配图时不要做「深色左半 + 浅色右半」——右半会是一整块空白。
        # 改成整幅主题色，用同色系的深浅分层造层次，观感完整。
        _rect(slide, left=0, top=0, width=W, height=H, color=theme["hero_bg"])
        _rect(slide, left=7.6, top=0, width=W - 7.6, height=H,
              color=_blend(theme["hero_bg"], _WHITE, 0.07))
        _rect(slide, left=7.6, top=0, width=0.075, height=H, color=theme["accent"])
        _rect(slide, left=0, top=0, width=0.2, height=H, color=theme["accent"])
        text_left, text_w = 1.1, 6.2

    frame = _textbox(slide, left=text_left, top=H - 2.72, width=text_w, height=1.6)
    para = _line(frame, deck.get("title") or "演示文稿", size=38, theme=theme,
                 color_key="hero_fg", bold=True, first=True, space_after=0, title=True)
    para.line_spacing = 1.12

    _rect(slide, left=text_left, top=H - 1.06, width=1.5, height=0.055,
          color=theme["accent"])

    if deck.get("subtitle"):
        sub = _textbox(slide, left=text_left, top=H - 0.92, width=text_w, height=0.7)
        _line(sub, deck["subtitle"], size=16, theme=theme, color_key="hero_fg",
              first=True, space_after=0)

    footer = deck.get("footer") or ""
    if footer:
        box = _textbox(slide, left=text_left, top=H - 0.46, width=text_w, height=0.4)
        _line(box, footer, size=11, theme=theme, color_key="fg_muted", first=True,
              space_after=0)


def _layout_section_divider(prs, slide_spec, theme, page, ctx=None, deck=None):
    """章节页：左侧照片/色块 + 右侧议程列表。

    章节页的作用是「换气 + 报位置」，所以右栏直接把本章要点列出来，
    而不是只放一个大标题。
    """
    slide = _blank(prs, theme)
    art = _img_for(ctx, slide_spec.get("headerImage"))
    left_w = 4.6
    painted = False
    if art:
        painted = _picture(slide, art, left=0, top=0, width=left_w, height=H, mode="dark")
    if not painted:
        _rect(slide, left=0, top=0, width=left_w, height=H, color=theme["hero_bg"])
    _rect(slide, left=0, top=0, width=0.2, height=H, color=theme["accent"])

    num = str(slide_spec.get("chapter") or "").strip()
    if num:
        nf = _textbox(slide, left=0.7, top=2.5, width=3.4, height=1.5)
        _line(nf, num, size=72, theme=theme, color_key="hero_fg", bold=True,
              first=True, space_after=0, title=True)

    tf = _textbox(slide, left=0.7, top=4.15, width=3.4, height=1.2)
    _line(tf, str(slide_spec.get("title") or "").strip(), size=22, theme=theme,
          color_key="hero_fg", bold=True, first=True, space_after=0, title=True)

    # 右栏：本章议程
    bullets = _bullets_of(slide_spec.get("bullets"), limit=6)
    if not bullets:
        bullets = [("（本章无要点）", 0)]
    right_left = left_w + 0.85
    right_w = W - right_left - BODY_LEFT
    n = len(bullets)
    # 议程行高按可用高度拉伸；分隔线必须跟着「这一行的底」走，
    # 写死固定 y 的话会横穿文字
    list_top = 3.3
    avail = H - list_top - 1.1
    row_h = min(0.78, avail / max(1, n))
    top0 = list_top + max(0.0, (avail - row_h * n) / 2)
    _rect(slide, left=right_left, top=top0 - 0.72, width=0.055, height=0.44,
          color=theme["accent"])
    hf = _textbox(slide, left=right_left + 0.24, top=top0 - 0.7, width=right_w,
                  height=0.44)
    _line(hf, str(slide_spec.get("subtitle") or "本章要点").strip(), size=13,
          theme=theme, color_key="fg_muted", bold=True, first=True, space_after=0)

    for index, (text, level) in enumerate(bullets):
        y = top0 + index * row_h
        tf = _textbox(slide, left=right_left, top=y, width=right_w, height=row_h)
        _line(tf, text, size=15 if not level else 13, theme=theme,
              color_key="fg" if not level else "fg_muted", first=True, space_after=0)
        if index < n - 1:
            _hairline(slide, right_left, y + row_h - 0.1, right_w, theme["line"])

    _footnote(slide, slide_spec, theme, page, (deck or {}).get("footer") or "")


# ----------------------------------------------------------------
# 内容版式
# ----------------------------------------------------------------


def _layout_title_bullets(prs, slide_spec, theme, page, ctx=None, deck=None):
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    bullets = _bullets_of(slide_spec.get("bullets"))
    if not bullets:
        bullets = [("（本页无要点）", 0)]
    size = _grow_size(bullets, BODY_WIDTH, bottom - BODY_TOP)
    height = sum(
        _text_height(t, size if not lv else max(11.0, size - 2), BODY_WIDTH,
                     11 if not lv else 7)
        for t, lv in bullets
    )
    top = _block_top(bottom - BODY_TOP, height, BODY_TOP)
    frame = _textbox(slide, left=BODY_LEFT + 0.32, top=top, width=BODY_WIDTH - 0.32,
                     height=max(0.8, bottom - top))
    for index, (text, level) in enumerate(bullets):
        para = _line(
            frame, text,
            size=size if not level else max(12.0, size - 2),
            theme=theme,
            color_key="fg" if not level else "fg_muted",
            first=(index == 0),
            space_after=11 if not level else 7,
        )
        para.level = level
    # 一级要点用小方块标记，比圆点更「方硬」，符合不用圆角的路子。
    # 位置按逐行累加算，否则字号变了方块就和文字错位。
    cursor = top
    for index, (text, level) in enumerate(bullets):
        line_h = _text_height(text, size if not level else max(11.0, size - 2),
                              BODY_WIDTH - 0.32, 11 if not level else 7)
        if not level:
            _rect(slide, left=BODY_LEFT + 0.04, top=cursor + 0.1,
                  width=0.11, height=0.11, color=theme["accent"])
        cursor += line_h


def _layout_columns(prs, slide_spec, theme, page, ctx=None, deck=None):
    """等宽多栏证据：每栏一个编号 + 小标题 + 细线 + 短段落。

    规范认可的一种高密度版式，三栏是「三个并列维度」的自然表达。
    """
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    cols = slide_spec.get("columns")
    if not isinstance(cols, list) or not cols:
        return _layout_title_bullets(prs, slide_spec, theme, page, ctx, deck)
    cols = [c for c in cols if isinstance(c, dict)][:4]
    n = len(cols)
    gap = 0.42
    col_w = (BODY_WIDTH - gap * (n - 1)) / n

    avail = bottom - BODY_TOP
    # 三栏之间拉通高的竖向细线，把正文区框成一个网格。
    # 这样即便某一栏文字短，空白读起来也是「栏内边距」，
    # 而不是「页面下半部分没填」—— 靠内容撑满反而会让字号失控。
    for i in range(1, n):
        _vline(slide, BODY_LEFT + i * (col_w + gap) - gap / 2, BODY_TOP,
               avail, theme["line"])

    head_h, rule_gap, body_gap = 0.74, 1.0, 1.16
    # 字号按「最高的一栏能否装下」逐档放大，尽量吃掉竖向空白
    body_size = 14.0
    for cand in (14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0):
        hs = [_text_height(str(c.get("body") or ""), cand, col_w - 0.08) for c in cols]
        if head_h + max(hs) + 0.5 <= avail:
            body_size = cand
        else:
            break
    heights = [
        _text_height(str(c.get("body") or ""), body_size, col_w - 0.08)
        for c in cols
    ]

    for i, col in enumerate(cols):
        left = BODY_LEFT + i * (col_w + gap)
        _marker(slide, cx=left + 0.17, cy=BODY_TOP + 0.18, r=0.17,
                text=str(col.get("index") or (i + 1)), theme=theme)
        hf = _textbox(slide, left=left, top=BODY_TOP + 0.46, width=col_w, height=0.62)
        _line(hf, str(col.get("heading") or "")[:40], size=17, theme=theme,
              color_key="title", bold=True, first=True, space_after=0)
        _hairline(slide, left, body_gap + BODY_TOP - 0.16, col_w, theme["line"])
        body = str(col.get("body") or "").strip()
        if body:
            bf = _textbox(slide, left=left, top=BODY_TOP + body_gap,
                          width=col_w, height=max(0.6, avail - body_gap - 0.9))
            _line(bf, body, size=body_size, theme=theme, first=True, space_after=0,
                  line_spacing=1.42)
        bullets = _bullets_of(col.get("bullets"), limit=6)
        if bullets:
            pf = _textbox(slide, left=left,
                          top=BODY_TOP + body_gap + max(heights) + 0.22,
                          width=col_w, height=max(0.6, avail - body_gap - max(heights) - 0.3))
            for k, (text, level) in enumerate(bullets):
                _line(pf, "· " + text, size=12.5 if not level else 11.5, theme=theme,
                      color_key="fg" if not level else "fg_muted",
                      first=(k == 0), space_after=8)


def _layout_metrics(prs, slide_spec, theme, page, ctx=None, deck=None):
    """指标卡：3-6 个 KPI，超大数字 + 标签 + 一行口径说明。

    规范明确把「指标卡」列为允许用卡片的场景之一
    （其余场景默认禁止卡片分组）。
    """
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    items = slide_spec.get("metrics")
    if not isinstance(items, list) or not items:
        return _layout_title_bullets(prs, slide_spec, theme, page, ctx, deck)
    items = [m for m in items if isinstance(m, dict)][:6]
    n = len(items)
    per_row = 3 if n > 2 else n
    rows = -(-n // per_row)
    gap = 0.34
    card_w = (BODY_WIDTH - gap * (per_row - 1)) / per_row
    avail = bottom - BODY_TOP
    # 单行时卡片可以更高；再整块竖直居中，避免下方空一条
    card_h = min(2.6 if rows == 1 else 2.05, (avail - gap * (rows - 1)) / rows)
    top0 = _block_top(avail, card_h * rows + gap * (rows - 1), BODY_TOP)
    for i, m in enumerate(items):
        r, c = divmod(i, per_row)
        left = BODY_LEFT + c * (card_w + gap)
        top = top0 + r * (card_h + gap)
        _rect(slide, left=left, top=top, width=card_w, height=card_h,
              color=theme["panel"])
        _rect(slide, left=left, top=top, width=card_w, height=0.05,
              color=theme["accent"])
        vf = _textbox(slide, left=left + 0.24, top=top + 0.24, width=card_w - 0.48,
                      height=0.92)
        _line(vf, str(m.get("value") or "")[:16], size=40, theme=theme,
              color_key="title", bold=True, first=True, space_after=0, title=True)
        lf = _textbox(slide, left=left + 0.24, top=top + 1.06, width=card_w - 0.48,
                      height=0.4)
        _line(lf, str(m.get("label") or "")[:34], size=13, theme=theme,
              color_key="fg", bold=True, first=True, space_after=0)
        note = str(m.get("note") or "").strip()
        if note:
            nf = _textbox(slide, left=left + 0.24, top=top + 1.46, width=card_w - 0.48,
                          height=max(0.3, card_h - 1.5))
            _line(nf, note[:90], size=10.5, theme=theme, color_key="fg_muted",
                  first=True, space_after=0, line_spacing=1.25)


def _layout_matrix(prs, slide_spec, theme, page, ctx=None, deck=None):
    """2×2 矩阵：两轴 + 四象限，标签直接写在象限里。

    象限用极浅底色区分即可，靠细线勾出网格 —— 不堆色块。
    """
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    spec = slide_spec.get("matrix") or {}
    if not isinstance(spec, dict):
        spec = {}
    quads = [q if isinstance(q, dict) else {} for q in (spec.get("quadrants") or [])][:4]
    while len(quads) < 4:
        quads.append({})

    grid_left, grid_top = 1.72, BODY_TOP + 0.14
    grid_w = min(7.4, BODY_WIDTH - 4.6)
    # 必须给下方 x 轴标签留出 0.6 英寸，否则标签会压到收束条上
    grid_h = min(bottom - grid_top - 0.62, 3.6)
    cw, ch = grid_w / 2, grid_h / 2
    # 先铺四个象限底色，再压细线，最后写字，避免线被色块盖住
    for i, q in enumerate(quads):
        r, c = divmod(i, 2)
        _rect(slide, left=grid_left + c * cw, top=grid_top + r * ch,
              width=cw, height=ch, color=theme["panel"])
    for r in range(3):
        _hairline(slide, grid_left, grid_top + r * ch, grid_w, theme["line"], 0.016)
    for c in range(3):
        _vline(slide, grid_left + c * cw, grid_top, grid_h, theme["line"], 0.016)

    for i, q in enumerate(quads):
        r, c = divmod(i, 2)
        ql = grid_left + c * cw + 0.2
        qt = grid_top + r * ch + 0.16
        label = str(q.get("label") or "").strip()
        if label:
            lf = _textbox(slide, left=ql, top=qt, width=cw - 0.4, height=0.42)
            _line(lf, label[:26], size=13, theme=theme, color_key="accent",
                  bold=True, first=True, space_after=0)
        desc = str(q.get("desc") or "").strip()
        if desc:
            df = _textbox(slide, left=ql, top=qt + 0.42, width=cw - 0.4,
                          height=ch - 0.6)
            _line(df, desc[:140], size=11, theme=theme, first=True, space_after=0,
                  line_spacing=1.25)

    # 轴标签
    _hairline(slide, grid_left, grid_top + grid_h + 0.12, grid_w, theme["fg_muted"], 0.02)
    _vline(slide, grid_left - 0.12, grid_top, grid_h, theme["fg_muted"], 0.02)
    xl = str(spec.get("xLabel") or "").strip()
    if xl:
        xf = _textbox(slide, left=grid_left, top=grid_top + grid_h + 0.2,
                      width=grid_w, height=0.4)
        _line(xf, xl[:80], size=11, theme=theme, color_key="fg_muted", first=True,
              space_after=0, align="center")
    yl = str(spec.get("yLabel") or "").strip()
    if yl:
        yf = _textbox(slide, left=0.28, top=grid_top + grid_h / 2 - 0.3, width=1.36,
                      height=0.6)
        _line(yf, yl[:40], size=11, theme=theme, color_key="fg_muted", first=True,
              space_after=0, align="right")

    # 右侧：读数说明
    notes = _bullets_of(slide_spec.get("bullets"), limit=5)
    if notes:
        nl = grid_left + grid_w + 0.5
        nw = W - BODY_LEFT - nl
        _rect(slide, left=nl, top=grid_top, width=0.055, height=0.44,
              color=theme["accent"])
        hf = _textbox(slide, left=nl + 0.22, top=grid_top + 0.02, width=nw - 0.22,
                      height=0.44)
        _line(hf, str(slide_spec.get("subtitle") or "怎么读这张图").strip(), size=12,
              theme=theme, color_key="fg_muted", bold=True, first=True, space_after=0)
        pf = _textbox(slide, left=nl, top=grid_top + 0.62, width=nw,
                      height=max(0.8, grid_h - 0.62))
        for k, (text, level) in enumerate(notes):
            _line(pf, "· " + text, size=11.5, theme=theme, color_key="fg",
                  first=(k == 0), space_after=9, line_spacing=1.25)


def _layout_timeline(prs, slide_spec, theme, page, ctx=None, deck=None):
    """横向时间轴：编号节点 + 竖线 + 名称/说明三层。

    轨道线用灰色细线承载，节点用主题色编号块 —— 不靠图例。
    """
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    nodes = slide_spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return _layout_title_bullets(prs, slide_spec, theme, page, ctx, deck)
    nodes = [n if isinstance(n, dict) else {"title": str(n)} for n in nodes][:6]
    n = len(nodes)
    step = BODY_WIDTH / n
    # 说明文字的字号逐档放大，直到整块接近填满可用高度
    avail = bottom - BODY_TOP
    desc_size = 11.0
    for cand in (11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0):
        hs = [_text_height(str(nd.get("desc") or ""), cand, step - 0.34) for nd in nodes]
        if 0.19 + 0.4 + 0.62 + 0.12 + max(hs, default=0.6) <= avail:
            desc_size = cand
        else:
            break
    desc_h = max(
        (_text_height(str(nd.get("desc") or ""), desc_size, step - 0.34) for nd in nodes),
        default=0.6,
    )
    block_h = 0.19 + 0.4 + 0.62 + 0.12 + desc_h
    track_y = _block_top(avail, block_h, BODY_TOP) + 0.19
    _hairline(slide, BODY_LEFT + 0.16, track_y, BODY_WIDTH - 0.32, theme["line"], 0.03)
    for i, node in enumerate(nodes):
        cx = BODY_LEFT + step * i + step / 2
        _marker(slide, cx=cx, cy=track_y + 0.015, r=0.19,
                text=str(node.get("index") or (i + 1)), theme=theme)
        w = step - 0.34
        left = cx - w / 2
        tf = _textbox(slide, left=left, top=track_y + 0.4, width=w, height=0.62)
        _line(tf, str(node.get("title") or "")[:26], size=15, theme=theme,
              color_key="title", bold=True, first=True, space_after=0, align="center")
        _hairline(slide, left, track_y + 1.1, w, theme["line"])
        desc = str(node.get("desc") or "").strip()
        if desc:
            df = _textbox(slide, left=left, top=track_y + 1.22, width=w,
                          height=max(0.6, bottom - track_y - 1.22))
            _line(df, desc[:120], size=desc_size, theme=theme, color_key="fg",
                  first=True, space_after=0, align="center", line_spacing=1.34)


def _layout_process(prs, slide_spec, theme, page, ctx=None, deck=None):
    """流程链：编号块 + 方向箭头，一格一个动作。"""
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    steps = slide_spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return _layout_title_bullets(prs, slide_spec, theme, page, ctx, deck)
    steps = [s if isinstance(s, dict) else {"title": str(s)} for s in steps][:5]
    n = len(steps)
    gap = 0.3
    bw = (BODY_WIDTH - gap * (n - 1)) / n
    top = BODY_TOP + 0.22
    # 原来写死 min(2.5, ...) 导致卡片下面空一大块；
    # 直接撑满正文区，卡片高度由版面决定
    block_h = bottom - top - 0.28
    for i, st in enumerate(steps):
        left = BODY_LEFT + i * (bw + gap)
        _rect(slide, left=left, top=top, width=bw, height=block_h, color=theme["panel"])
        _rect(slide, left=left, top=top, width=bw, height=0.05, color=theme["accent"])
        _marker(slide, cx=left + 0.34, cy=top + 0.46, r=0.19,
                text=str(st.get("index") or (i + 1)), theme=theme)
        tf = _textbox(slide, left=left + 0.22, top=top + 0.82, width=bw - 0.44,
                      height=0.62)
        _line(tf, str(st.get("title") or "")[:24], size=14, theme=theme,
              color_key="title", bold=True, first=True, space_after=0)
        desc = str(st.get("desc") or "").strip()
        if desc:
            df = _textbox(slide, left=left + 0.22, top=top + 1.44, width=bw - 0.44,
                          height=max(0.5, block_h - 1.5))
            _line(df, desc[:110], size=11, theme=theme, color_key="fg",
                  first=True, space_after=0, line_spacing=1.25)
        if i < n - 1:
            _chevron(slide, left=left + bw + 0.02, top=top + block_h / 2 - 0.12,
                     width=0.26, height=0.24, color=theme["accent"])


def _layout_contents(prs, slide_spec, theme, page, ctx=None, deck=None):
    """目录：编号 + 章节名 + 页码提示，一行一章。"""
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    items = slide_spec.get("items") or slide_spec.get("bullets")
    entries = _bullets_of(items, limit=10)
    if not entries:
        entries = [("（暂无目录）", 0)]
    n = len(entries)
    avail = bottom - BODY_TOP - 0.16
    # 目录条目天然少（4-6 条），固定行高会在下面留一大块空白，
    # 所以行高随可用高度拉伸，并给少条目放大字号
    row_h = min(1.08, avail / max(1, n))
    top = BODY_TOP + 0.16 + max(0.0, (avail - row_h * n) / 2)
    num_size = 20 if n <= 5 else 17
    txt_size = 17 if n <= 5 else 15
    for i, (text, level) in enumerate(entries):
        y = top + i * row_h
        num = "%02d" % (i + 1)
        # 行内内容竖直居中：顶对齐时最后一行文字会浮在行的上半部，
        # 下面看着像漏了一块
        inner = y + max(0.0, (row_h - 0.46) / 2)
        nf = _textbox(slide, left=BODY_LEFT, top=inner, width=0.85, height=0.46)
        _line(nf, num, size=num_size, theme=theme, color_key="accent", bold=True,
              first=True, space_after=0, title=True)
        tf = _textbox(slide, left=BODY_LEFT + 0.9, top=inner, width=BODY_WIDTH - 0.9,
                      height=0.46)
        _line(tf, text[:70], size=txt_size, theme=theme, color_key="title", bold=True,
              first=True, space_after=0)
        if i < len(entries) - 1:
            _hairline(slide, BODY_LEFT, y + row_h - 0.04, BODY_WIDTH, theme["line"])


def _layout_summary(prs, slide_spec, theme, page, ctx=None, deck=None):
    """结论页：编号 + 加粗主张 + 一行解释。

    结尾要放「编号的结论」（主张），而不是「总结」。
    """
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    claims = _bullets_of(slide_spec.get("bullets") or slide_spec.get("claims"), limit=6)
    if not claims:
        claims = [("（暂无结论）", 0)]
    top = BODY_TOP + 0.12
    # 结论条数少（4-5 条）时，行高上限太小会在下方留一条空白
    row_h = min(1.12, (bottom - top) / max(1, len(claims)))
    for i, (text, level) in enumerate(claims):
        y = top + i * row_h
        _marker(slide, cx=BODY_LEFT + 0.19, cy=y + 0.19, r=0.19,
                text=str(i + 1), theme=theme)
        tf = _textbox(slide, left=BODY_LEFT + 0.56, top=y, width=BODY_WIDTH - 0.56,
                      height=row_h)
        _line(tf, text, size=15.5, theme=theme, color_key="fg", bold=True,
              first=True, space_after=0, line_spacing=1.2)


def _layout_two_column(prs, slide_spec, theme, page, ctx=None, deck=None):
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    col_w = (BODY_WIDTH - 0.7) / 2
    for key, left in (("left", BODY_LEFT), ("right", BODY_LEFT + col_w + 0.7)):
        col = slide_spec.get(key) or {}
        if not isinstance(col, dict):
            col = {"bullets": [col]}
        if key == "right":
            _vline(slide, BODY_LEFT + col_w + 0.33, BODY_TOP + 0.05, bottom - BODY_TOP - 0.1,
                   theme["line"])
        top = BODY_TOP + 0.04
        heading = col.get("heading")
        if heading:
            _rect(slide, left=left, top=top + 0.04, width=0.055, height=0.32,
                  color=theme["accent"])
            hf = _textbox(slide, left=left + 0.2, top=top, width=col_w - 0.2, height=0.48)
            _line(hf, str(heading)[:36], size=16, theme=theme, color_key="title",
                  bold=True, first=True, space_after=0)
            top += 0.58
        bullets = _bullets_of(col.get("bullets"), limit=9)
        if not bullets:
            continue
        size = _bullets_size(bullets, col_w)
        frame = _textbox(slide, left=left, top=top, width=col_w,
                         height=max(0.6, bottom - top))
        for index, (text, level) in enumerate(bullets):
            para = _line(frame, text, size=size if not level else max(11.5, size - 2),
                         theme=theme, color_key="fg" if not level else "fg_muted",
                         first=(index == 0), space_after=8)
            para.level = level
            if not level:
                _rect(slide, left=left - 0.2, top=top + 0.1 + index * 0.32,
                      width=0.09, height=0.09, color=theme["accent"])


def _layout_big_number(prs, slide_spec, theme, page, ctx=None, deck=None):
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    number = str(slide_spec.get("number") or "").strip() or "—"
    _rect(slide, left=BODY_LEFT, top=BODY_TOP + 0.42, width=0.075, height=2.3,
          color=theme["accent"])
    nf = _textbox(slide, left=BODY_LEFT + 0.34, top=BODY_TOP + 0.3, width=8.6, height=1.8)
    _line(nf, number, size=84, theme=theme, color_key="title", bold=True,
          first=True, space_after=0, title=True)
    caption = str(slide_spec.get("caption") or "").strip()
    if caption:
        cf = _textbox(slide, left=BODY_LEFT + 0.38, top=BODY_TOP + 2.42, width=10.4,
                      height=1.4)
        _line(cf, caption, size=17, theme=theme, first=True, space_after=0,
              line_spacing=1.3)


def _layout_quote(prs, slide_spec, theme, page, ctx=None, deck=None):
    slide = _blank(prs, theme)
    # 整版 hero 色块 + 反色文字：深色主题下是深底亮字，浅色主题下是深底白字，
    # 两种都成立，不需要为深色另外写一套
    _rect(slide, left=0, top=0, width=W, height=H, color=theme["hero_bg"])
    _rect(slide, left=0, top=0, width=0.2, height=H, color=theme["accent"])
    mark = _textbox(slide, left=0.85, top=0.5, width=2.0, height=1.5)
    _line(mark, "\u201c", size=92, theme=theme, color_key="accent", bold=True,
          first=True, space_after=0)
    quote = str(slide_spec.get("quote") or slide_spec.get("title") or "").strip()
    qf = _textbox(slide, left=1.3, top=1.85, width=10.6, height=3.1)
    size = 30.0 if len(quote) <= 70 else (24.0 if len(quote) <= 130 else 19.0)
    _line(qf, quote, size=size, theme=theme, color_key="hero_fg", first=True,
          space_after=0, title=True, line_spacing=1.28)
    author = str(slide_spec.get("author") or "").strip()
    if author:
        # 出处跟着引文实际高度走：固定贴底时，引文只有一行会空出一大块
        quote_h = _text_height(quote, size, 10.6)
        af = _textbox(slide, left=1.3, top=min(5.7, 1.85 + quote_h + 0.4), width=10.6,
                      height=0.5)
        _line(af, "—— " + author, size=14, theme=theme, color_key="accent",
              first=True, space_after=0, align="right")
    _footnote(slide, slide_spec, theme, page, (deck or {}).get("footer") or "")


def _layout_title_only(prs, slide_spec, theme, page, ctx=None, deck=None):
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    title = str(slide_spec.get("title") or "").strip()
    sub = str(slide_spec.get("subtitle") or slide_spec.get("scope") or "").strip()
    tf = _textbox(slide, left=BODY_LEFT, top=BODY_TOP + 0.5, width=BODY_WIDTH, height=1.8)
    size = 38.0 if len(title) <= 22 else (30.0 if len(title) <= 40 else 25.0)
    _line(tf, title, size=size, theme=theme, color_key="title", bold=True,
          first=True, space_after=0, align="center", title=True)
    _rect(slide, left=W / 2 - 0.5, top=BODY_TOP + 2.42, width=1.0, height=0.055,
          color=theme["accent"])
    if sub:
        sf = _textbox(slide, left=BODY_LEFT, top=BODY_TOP + 2.66, width=BODY_WIDTH,
                      height=1.3)
        _line(sf, sub, size=16, theme=theme, color_key="fg_muted", first=True,
              space_after=0, align="center", line_spacing=1.3)


_CHART_TYPES = {"bar": "COLUMN_CLUSTERED", "column": "COLUMN_CLUSTERED",
                "line": "LINE_MARKERS", "pie": "PIE", "barh": "BAR_CLUSTERED",
                "stacked": "COLUMN_STACKED", "area": "AREA"}


def _layout_chart(prs, slide_spec, theme, page, ctx=None, deck=None):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Inches, Pt

    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    spec = slide_spec.get("chart") or {}
    if not isinstance(spec, dict):
        spec = {}
    chart_h = (bottom - BODY_TOP - 0.1) * (0.82 if str(slide_spec.get("takeaway") or "").strip() else 0.94)

    data = CategoryChartData()
    data.categories = [str(c)[:24] for c in (spec.get("categories") or [])]
    for series in (spec.get("series") or [])[:4]:
        if not isinstance(series, dict):
            continue
        values = []
        for v in (series.get("values") or [])[:24]:
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                values.append(0.0)
        data.add_series(str(series.get("name") or "")[:28], values)

    kind = str(spec.get("type") or "bar").lower()
    chart_type = getattr(XL_CHART_TYPE, _CHART_TYPES.get(kind, "COLUMN_CLUSTERED"))
    gf = slide.shapes.add_chart(
        chart_type, Inches(BODY_LEFT), Inches(BODY_TOP + 0.02),
        Inches(BODY_WIDTH), Inches(chart_h), data,
    )
    chart = gf.chart
    chart.has_title = False
    if len(list(chart.series)) > 1:
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    else:
        chart.has_legend = False
    try:
        chart.font.size = Pt(11)
        chart.font.name = theme.get("font_body") or "微软雅黑"
        chart.font.color.rgb = _rgb(theme["fg"])
    except Exception:  # noqa: BLE001
        pass

    # 不指定颜色的话图表会用 python-pptx 内置默认蓝，和主题打架
    palette = [theme["band"], theme["accent"], theme["fg_muted"]]
    for index, series in enumerate(chart.series):
        color = _rgb(palette[index % len(palette)])
        try:
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = color
        except Exception:  # noqa: BLE001 折线图没有填充
            pass
        try:
            series.format.line.color.rgb = color
        except Exception:  # noqa: BLE001
            pass

    # 网格线压到几乎看不见：规范要求图表背景透明、不抢数据重心
    try:
        chart.value_axis.major_gridlines.format.line.color.rgb = _rgb(theme["line"])
    except Exception:  # noqa: BLE001
        pass
    try:
        chart.category_axis.tick_labels.font.color.rgb = _rgb(theme["fg_muted"])
        chart.value_axis.tick_labels.font.color.rgb = _rgb(theme["fg_muted"])
        chart.category_axis.format.line.color.rgb = _rgb(theme["line"])
        chart.value_axis.format.line.color.rgb = _rgb(theme["line"])
    except Exception:  # noqa: BLE001
        pass
    # 单序列直接把数值标在柱子上，满足「不靠图例」的直接标注要求
    try:
        if len(list(chart.series)) == 1 and chart_type != XL_CHART_TYPE.PIE:
            plot = chart.plots[0]
            plot.has_data_labels = True
            plot.data_labels.font.size = Pt(10)
            plot.data_labels.font.color.rgb = _rgb(theme["fg"])
    except Exception:  # noqa: BLE001
        pass

    # 没有收束条时，图下补一行读数提示，避免下半页空着
    if not str(slide_spec.get("takeaway") or "").strip():
        hint = str(spec.get("takeaway") or slide_spec.get("scope") or "").strip()
        if hint:
            hf = _textbox(slide, left=BODY_LEFT, top=BODY_TOP + chart_h + 0.1,
                          width=BODY_WIDTH, height=0.7)
            _line(hf, "读数：" + hint, size=12, theme=theme, color_key="accent",
                  bold=True, first=True, space_after=0)


def _layout_table(prs, slide_spec, theme, page, ctx=None, deck=None):
    from pptx.util import Inches

    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    spec = slide_spec.get("table") or {}
    if not isinstance(spec, dict):
        spec = {}
    headers = [str(h)[:20] for h in (spec.get("headers") or [])][:6]
    rows = [[str(c)[:28] for c in (r or [])][: len(headers)]
            for r in (spec.get("rows") or [])][:12]
    if not headers or not rows:
        return _layout_title_bullets(prs, slide_spec, theme, page, ctx, deck)

    avail = bottom - BODY_TOP - 0.12
    row_h = min(0.44, avail / (len(rows) + 1))
    shape = slide.shapes.add_table(
        len(rows) + 1, len(headers), Inches(BODY_LEFT), Inches(BODY_TOP + 0.04),
        Inches(BODY_WIDTH), Inches(row_h * (len(rows) + 1)),
    )
    table = shape.table
    # 关掉内置样式的首行深底和隔行条带：那套蓝色和主题不搭，
    # 而且规范要求「细边框、少填充」，所以逐格自己上色
    try:
        table.first_row = False
        table.horz_banding = False
    except Exception:  # noqa: BLE001
        pass
    for c, text in enumerate(headers):
        cell = table.cell(0, c)
        cell.text = text
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(theme["hero_bg"])
        cell.margin_top = cell.margin_bottom = Inches(0.02)
        for para in cell.text_frame.paragraphs:
            for run in para.runs:
                _font(run, size=12.5, theme=theme, color_key="hero_fg", bold=True)
    for r, row in enumerate(rows, 1):
        for c in range(len(headers)):
            cell = table.cell(r, c)
            cell.text = row[c] if c < len(row) else ""
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme["panel"] if r % 2 == 0 else theme["bg"])
            cell.margin_top = cell.margin_bottom = Inches(0.02)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    _font(run, size=12, theme=theme,
                          bold=(c == 0))


def _layout_image_right(prs, slide_spec, theme, page, ctx=None, deck=None):
    """左文右图。配图拿不到时自动变成整宽要点页，绝不空一块。"""
    slide, bottom = _content_frame(prs, slide_spec, theme, page, ctx, deck)
    art = _img_for(ctx, slide_spec.get("imagePrompt"))
    bullets = _bullets_of(slide_spec.get("bullets"))
    if art:
        img_w = 4.9
        img_h = min(bottom - BODY_TOP - 0.06, img_w * 0.72)
        img_left = W - BODY_LEFT - img_w
        img_top = BODY_TOP + 0.08
        _rect(slide, left=img_left - 0.1, top=img_top - 0.1, width=img_w + 0.2,
              height=img_h + 0.2, color=theme["panel"])
        _picture(slide, art, left=img_left, top=img_top, width=img_w, height=img_h)
        text_w = img_left - BODY_LEFT - 0.55
    else:
        text_w = BODY_WIDTH
    if not bullets:
        bullets = [("（本页无要点）", 0)]
    size = _bullets_size(bullets, text_w)
    height = sum(_text_height(t, size if not lv else size - 2, text_w, 11 if not lv else 7)
                 for t, lv in bullets)
    top = BODY_TOP + min(0.8, max(0.0, (bottom - BODY_TOP - height) * 0.5))
    frame = _textbox(slide, left=BODY_LEFT + 0.3, top=top, width=text_w - 0.3,
                     height=max(0.8, bottom - top))
    for index, (text, level) in enumerate(bullets):
        para = _line(frame, text, size=size if not level else max(12.0, size - 2),
                     theme=theme, color_key="fg" if not level else "fg_muted",
                     first=(index == 0), space_after=11 if not level else 7)
        para.level = level
        if not level:
            _rect(slide, left=BODY_LEFT + 0.02, top=top + 0.11 + index * 0.34,
                  width=0.1, height=0.1, color=theme["accent"])


def _layout_image_full(prs, slide_spec, theme, page, ctx=None, deck=None):
    """整页配图 + 底部实色标题带。

    不用半透明蒙版：python-pptx 设 alpha 要改 XML，容易踩坑；
    实色带既保证可读，又不依赖透明效果。
    """
    art = _img_for(ctx, slide_spec.get("imagePrompt"))
    if not art:
        return _layout_section_divider(prs, slide_spec, theme, page, ctx, deck)
    slide = _blank(prs, theme)
    _picture(slide, art, left=0, top=0, width=W, height=H, mode="dark")
    band_h = 2.15
    _rect(slide, left=0, top=H - band_h, width=W, height=band_h, color=theme["hero_bg"])
    _rect(slide, left=0, top=H - band_h, width=0.2, height=band_h, color=theme["accent"])
    tf = _textbox(slide, left=1.1, top=H - band_h + 0.4, width=11.2, height=0.9)
    _line(tf, str(slide_spec.get("title") or "").strip(), size=28, theme=theme,
          color_key="hero_fg", bold=True, first=True, space_after=0, title=True)
    sub = str(slide_spec.get("subtitle") or slide_spec.get("scope") or "").strip()
    if sub:
        sf = _textbox(slide, left=1.1, top=H - band_h + 1.32, width=11.2, height=0.6)
        _line(sf, sub, size=14, theme=theme, color_key="accent", first=True,
              space_after=0)
    _footnote(slide, slide_spec, theme, page, (deck or {}).get("footer") or "")


_LAYOUTS = {
    "title-bullets": _layout_title_bullets,
    "bullets": _layout_title_bullets,
    "columns": _layout_columns,
    "metrics": _layout_metrics,
    "matrix": _layout_matrix,
    "timeline": _layout_timeline,
    "process": _layout_process,
    "contents": _layout_contents,
    "summary": _layout_summary,
    "two-column": _layout_two_column,
    "big-number": _layout_big_number,
    "quote": _layout_quote,
    "section-divider": _layout_section_divider,
    "section": _layout_section_divider,
    "title-only": _layout_title_only,
    "chart": _layout_chart,
    "table": _layout_table,
    "image-right": _layout_image_right,
    "image-full": _layout_image_full,
}

LAYOUT_NAMES = [
    "title-bullets", "columns", "two-column", "metrics", "matrix",
    "timeline", "process", "chart", "table", "contents",
    "summary", "big-number", "quote", "section-divider", "title-only",
    "image-right", "image-full",
]

# 需要配图的版式：执行器据此决定要生成哪几张图
IMAGE_LAYOUTS = ("image-right", "image-full")

# 配图统一追加的负面约束：图像模型画文字必糊，直接禁掉
IMAGE_NEGATIVE = "，扁平插画风格，构图简洁，不要出现任何文字、字母、数字、水印或logo"


# ----------------------------------------------------------------
# 规范化 / 校验 / 渲染
# ----------------------------------------------------------------


def _clean(value: Any, limit: int = 100) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))
    return text.strip()[:limit]


def normalize_deck(args: dict) -> dict:
    """把模型给的参数规范成渲染器认识的结构。"""
    theme_name = _clean(args.get("theme"), 30).lower()
    if theme_name not in THEMES:
        theme_name = DEFAULT_THEME

    slides = []
    for item in list(args.get("slides") or [])[:40]:
        if not isinstance(item, dict):
            continue
        layout = _clean(item.get("layout"), 24).lower() or "title-bullets"
        if layout not in _LAYOUTS:
            layout = "title-bullets"
        spec = dict(item)
        spec["layout"] = layout
        spec["title"] = _clean(item.get("title"), 120)
        spec["subtitle"] = _clean(item.get("subtitle"), 160)
        spec["scope"] = _clean(item.get("scope"), 120)
        spec["chapter"] = _clean(item.get("chapter"), 8)
        spec["section"] = _clean(item.get("section"), 40)
        spec["takeaway"] = _clean(item.get("takeaway"), 160)
        spec["headerImage"] = _clean(item.get("headerImage"), 300)
        slides.append(spec)

    return {
        "coverImage": _clean(args.get("coverImage"), 300),
        "title": _clean(args.get("title"), 120) or "演示文稿",
        "subtitle": _clean(args.get("subtitle"), 160),
        "footer": _clean(args.get("footer"), 90),
        "theme": theme_name,
        "slides": slides,
    }


def lint_deck(deck: dict) -> list[str]:
    """渲染前检查，返回需要模型改的问题（空列表表示可以渲染）。

    这是「多轮打磨」的抓手：与其生成一份有问题的再让它改，
    不如把问题直接回给模型，让它带着具体修改意见重来一次。
    """
    problems: list[str] = []
    slides = deck.get("slides") or []
    if not slides:
        return ["slides 为空，至少要有一页内容。"]

    for i, spec in enumerate(slides, 1):
        layout = spec.get("layout")
        label = "第 " + str(i) + " 页（" + str(layout) + "）"
        if layout in ("title-bullets", "bullets"):
            if not _bullets_of(spec.get("bullets")):
                problems.append(label + "：bullets 为空，补上要点，或改用 title-only。")
            elif len(_bullets_of(spec.get("bullets"), 99)) > 8:
                problems.append(label + "：要点超过 8 条，拆成两页。")
        elif layout == "columns":
            cols = spec.get("columns")
            if not isinstance(cols, list) or len([c for c in cols if isinstance(c, dict)]) < 2:
                problems.append(label + "：columns 需要 2-4 个对象，每项含 heading 与 body 或 bullets。")
        elif layout == "metrics":
            items = spec.get("metrics")
            if not isinstance(items, list) or not items:
                problems.append(label + "：metrics 需要 metrics 数组，每项含 value 与 label。")
            elif len(items) > 6:
                problems.append(label + "：metrics 最多 6 个。")
            elif any(isinstance(m, dict) and not _clean(m.get("value"), 20) for m in items):
                problems.append(label + "：有指标缺 value。")
        elif layout == "matrix":
            m = spec.get("matrix") or {}
            if not isinstance(m, dict) or len([q for q in (m.get("quadrants") or []) if isinstance(q, dict)]) < 4:
                problems.append(label + "：matrix.quadrants 需要 4 个象限，每项含 label 与 desc。")
        elif layout == "timeline":
            nodes = spec.get("nodes")
            if not isinstance(nodes, list) or len(nodes) < 2:
                problems.append(label + "：timeline 需要 2-6 个 nodes，每项含 title 与 desc。")
        elif layout == "process":
            steps = spec.get("steps")
            if not isinstance(steps, list) or len(steps) < 2:
                problems.append(label + "：process 需要 2-5 个 steps，每项含 title 与 desc。")
        elif layout == "contents":
            if not _bullets_of(spec.get("items") or spec.get("bullets")):
                problems.append(label + "：contents 需要 items（每个章节一行）。")
        elif layout == "summary":
            if not _bullets_of(spec.get("bullets") or spec.get("claims")):
                problems.append(label + "：summary 需要 bullets（每条一句结论）。")
        elif layout == "two-column":
            if not isinstance(spec.get("left"), dict) or not isinstance(spec.get("right"), dict):
                problems.append(label + "：two-column 需要 left 和 right 两个对象，各含 heading/bullets。")
        elif layout == "big-number":
            if not _clean(spec.get("number"), 24):
                problems.append(label + "：big-number 缺少 number 字段。")
        elif layout == "quote":
            if not _clean(spec.get("quote") or spec.get("title"), 400):
                problems.append(label + "：quote 缺少 quote 字段。")
        elif layout == "chart":
            chart = spec.get("chart") or {}
            if not isinstance(chart, dict) or not (chart.get("categories") and chart.get("series")):
                problems.append(label + "：chart 需要 categories 与 series（series 里每项含 name/values）。")
            else:
                for s in chart.get("series") or []:
                    if isinstance(s, dict) and len(s.get("values") or []) != len(chart.get("categories") or []):
                        problems.append(label + "：categories 与 values 长度不一致。")
                        break
        elif layout in IMAGE_LAYOUTS:
            if not _clean(spec.get("imagePrompt"), 300):
                problems.append(label + "：缺少 imagePrompt（描述画面内容，不要写 URL）。")
        elif layout == "table":
            table = spec.get("table") or {}
            if not isinstance(table, dict) or not table.get("headers") or not table.get("rows"):
                problems.append(label + "：table 需要 headers 与 rows。")
        if layout not in ("quote", "section-divider", "title-only", "image-full") and not _clean(spec.get("title"), 120):
            problems.append(label + "：缺少 title。")
    return problems


def render_deck(path, deck: dict, images: dict | None = None) -> list[str]:
    """渲染整份 PPT，返回软警告（不影响交付）。

    images 是「图片描述 -> 本地图片路径」的映射，由调用方异步准备好后传进来。
    渲染本身是纯同步的，不在这里发网络请求。
    """
    from pptx import Presentation
    from pptx.util import Inches

    theme = dict(THEMES[deck.get("theme") or DEFAULT_THEME])
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)

    ctx = {"images": images or {}}
    warnings: list[str] = []
    if deck.get("coverImage") and not _img_for(ctx, deck.get("coverImage")):
        warnings.append("封面配图没拿到，封面已降级为纯色块")
    _cover(prs, deck, theme, ctx)

    for page, spec in enumerate(deck.get("slides") or [], 1):
        layout = spec.get("layout") or "title-bullets"
        render = _LAYOUTS.get(layout) or _layout_title_bullets
        if layout in IMAGE_LAYOUTS and not _img_for(ctx, spec.get("imagePrompt")):
            warnings.append("第 " + str(page) + " 页配图没拿到，已降级为文字版式")
        try:
            render(prs, spec, theme, page, ctx, deck)
        except Exception as exc:  # noqa: BLE001 — 单页失败不放弃整份
            warnings.append("第 " + str(page) + " 页渲染失败，已降级为要点页：" + type(exc).__name__)
            try:
                _layout_title_bullets(prs, spec, theme, page, ctx, deck)
            except Exception:  # noqa: BLE001
                _blank(prs, theme)

    prs.save(str(path))
    return warnings


def image_prompts(deck: dict) -> list[str]:
    """收集这份 deck 需要生成的图片描述（去重、保持顺序）。

    只取配图版式上的描述：不生成用不到的图，避免白花时间与额度。
    """
    seen: list[str] = []
    cover = str(deck.get("coverImage") or "").strip()
    if cover:
        seen.append(cover)
    for spec in deck.get("slides") or []:
        if spec.get("layout") not in IMAGE_LAYOUTS:
            continue
        prompt = str(spec.get("imagePrompt") or "").strip()
        if prompt and prompt not in seen:
            seen.append(prompt)
    return seen


def deck_summary(deck: dict) -> str:
    """给模型回执用的一句话摘要。"""
    counts: dict[str, int] = {}
    for spec in deck.get("slides") or []:
        counts[spec.get("layout") or "?"] = counts.get(spec.get("layout") or "?", 0) + 1
    parts = [k + "x" + str(v) for k, v in counts.items()]
    label = (THEMES.get(deck.get("theme")) or {}).get("label") or deck.get("theme")
    return str(len(deck.get("slides") or [])) + " 页 · " + str(label) + "（" + "、".join(parts) + "）"


def theme_menu() -> str:
    """把主题清单渲染成给模型看的文本。

    从 THEMES 生成而不是写死在提示词里：加了新主题不会再漏改提示词。
    """
    return "\n".join(
        "- " + name + " " + cfg["label"] + "：" + cfg["use"]
        for name, cfg in THEMES.items()
    )


__all__ = [
    "DEFAULT_THEME",
    "IMAGE_LAYOUTS",
    "IMAGE_NEGATIVE",
    "LAYOUT_NAMES",
    "THEMES",
    "deck_summary",
    "image_prompts",
    "lint_deck",
    "normalize_deck",
    "render_deck",
    "theme_menu",
]
