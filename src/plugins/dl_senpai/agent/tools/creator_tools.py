"""Creator Layer：把「群友想要一份文档」变成真实文件。

为什么要单独开一层，而不是把 file_tools 开放给所有群友：
    file_tools 里的 write_file / edit_file 接受**任意路径 + 任意内容**，
    开放给群友等于给每个人一个远程代码执行入口 —— 这既不是门一要的
    「群友能生成东西」，也违反「群友不能改学姐的代码」。

    所以这一层只接受**结构化文本**（标题、段落、要点），
    文件名由系统生成，输出目录写死，参数里没有任何路径或命令。
    能力是「让学姐写一份文档」，不是「在磁盘上写文件」。

发送：文档生成后通过 OneBot 的 upload_group_file / upload_private_file
    直接上传到当前会话 —— 不走 reply_send 的图片段，
    因为 OneBot 的文件上传是 API 调用而不是消息段。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ...pptx_design import (
    IMAGE_NEGATIVE,
    LAYOUT_NAMES,
    THEMES,
    deck_summary,
    image_prompts,
    lint_deck,
    normalize_deck,
    render_deck,
    theme_menu,
)
from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

logger = logging.getLogger("dl_senpai.tools.creator")

# 归属层。与 high_risk_layer / memory_layer / knowledge_layer 平级，
# 这样「8 个内置工具」的契约不会被打破。
CREATOR_OWNER = "creator_layer"

DOCX_MARKER = "DOCX"
PPTX_MARKER = "PPTX"
READDOC_MARKER = "READDOC"
DECK_MARKER = "DECK"

# 生成物落盘目录：固定位置，**不接受参数指定**。
# 本文件在 dl_senpai/agent/tools/ 下，向上 5 层才是仓库根
# （dl_senpai 直属文件用的是 parents[3]，这里多了 agent/tools 两层）。
_ROOT = Path(__file__).resolve().parents[5]
_GENERATED_DIR = _ROOT / "data" / "dl_senpai" / "generated"

# 上限：防止（或被诱导的）模型塞进几万字把文件与回复一起拖死。
_MAX_TITLE = 100
_MAX_SECTIONS = 60
_MAX_TEXT = 4000

DOCX_PROMPT_TEXT = """你可以写一份 Word 文档（.docx）并发给对方。

当对方说“帮我写一份/整理成文档/做个 Word/写个报告”时，用：
<<<DOCX {"title": "文档标题", "sections": [{"heading": "小节标题", "body": "小节正文"}]}>>>

规则：
- 内容取自对方给你的材料；材料不够就照实说，不要编造数据
- sections 是数组，每项含 heading 与 body；不要用一节塞完全文
- 只写文本内容，不要写文件路径、不要写代码、不要写命令
- 生成后会直接把文件发到这个会话里，你不用再描述它长什么样"""

PPTX_PROMPT_TEXT = """你可以做一份 PPT（.pptx）并发给对方。

**先看这里**：如果你手里只有一个主题、还没想好每页放什么，用 make_deck 更好 ——
那个只交代主题和要求，成品完整度高得多。这个能力适合两种情况：
你已经把每页的结构想清楚了，或者需要某个特定版式（矩阵、时间轴、表格、图表）。

系统负责排版（配色、字号、留白、页码、网格线、图表着色），**你不用管**。
你只做一件事：给每一页选一个版式、把内容填进去、补齐页眉页脚那几个字段。
所以成品好不好取决于**结构**：标题是不是成句、章节有没有递进、每页有没有结论 ——
而不是词写得多花。

调用格式：
<<<PPTX {"title": "演示标题", "subtitle": "副标题", "theme": "academic", "footer": "出处/单位", "slides": [...]}>>>

theme 从下面这几套里选，按内容场景挑最贴的（不写默认 academic）：
<<THEMES>>

==================== 每一页的公共字段 ====================
除纯视觉页外，内容页都要带上这几个字段，它们直接决定页面的「专业度」：

  chapter   章节码，两位数字，如 "01" "02"，同一章的页写同一个码
  section   小节名，2-8 字，如 "分页机制"
  title     页标题，**必须是完整结论句**，不是话题词
            ✗「分页机制」   ✓「三个层次共同支撑地址转换」
  scope     一行说明：这页讲什么范围、怎么读，10-30 字
  takeaway  底部收束条，一句话 so-what（「所以呢」），10-30 字

补齐这几项后，页面会自动带上「标题带 → 章节定位条 → 正文 → 结论条 → 脚注页码」
的完整骨架。**只读所有 title 应该能听懂整个故事线**；读不通就说明结构没搭对。

==================== 版式（layout） ====================
可选：<<LAYOUTS>>

----- 结构类 -----
contents   目录，开场用一页
  {"layout":"contents","title":"本讲结构","chapter":"00","section":"目录","scope":"共四章","items":["虚拟内存的基本概念","分页机制的实现"]}
section-divider  章节分隔页，每换一章用一页
  {"layout":"section-divider","chapter":"02","title":"分页机制","subtitle":"本章议程","bullets":["页表与 TLB","缺页处理流程"]}
summary   结尾结论页，**放编号的主张，不是总结**
  {"layout":"summary","title":"四点结论","chapter":"04","section":"小结","scope":"本讲要记住的","bullets":["虚拟内存的本质是用磁盘换地址空间的连续性","TLB 是分页性能的第一杠杆"]}

----- 论证类 -----
title-bullets  最常用。标题 + 要点
  {"layout":"title-bullets","title":"页标题","bullets":["要点一","- 二级说明"]}
columns  2-4 等宽栏并列论证，适合「三个层次」「三个方案」
  {"layout":"columns","title":"页标题","columns":[{"heading":"页表","body":"每个进程一张，记录映射关系。"},{"heading":"TLB","body":"页表的高速缓存。"}]}
two-column  左右对比、优缺点、A/B 方案
  {"layout":"two-column","title":"两种方案","left":{"heading":"方案A","bullets":["…"]},"right":{"heading":"方案B","bullets":["…"]}}
metrics  3-6 个关键指标卡。有 KPI 就用它，比文字罗列好看得多
  {"layout":"metrics","title":"实测数据","metrics":[{"value":"98.2%","label":"TLB 命中率","note":"循环遍历数组的典型负载"}]}
matrix  2×2 矩阵，适合「两个维度决定四种情况」
  {"layout":"matrix","title":"页标题","matrix":{"xLabel":"局部性 → 弱","yLabel":"开销 → 高","quadrants":[{"label":"象限名","desc":"说明"}]}}
  quadrants 必须正好 4 个，顺序为：左上、右上、左下、右下
timeline  横向时间轴，2-6 个节点
  {"layout":"timeline","title":"页标题","nodes":[{"index":"1","title":"阶段名","desc":"一句话"}]}
process  流程链，2-5 个步骤，带箭头连接
  {"layout":"process","title":"页标题","steps":[{"title":"步骤名","desc":"一句话"}]}
chart  有数据就用它，系统画原生图表（在 Office 里可以直接改）
  {"layout":"chart","title":"页标题","chart":{"type":"bar","categories":["一班","二班"],"series":[{"name":"平均分","values":[78,85]}]}}
  type 可选 bar / barh / line / pie / stacked / area
table  行列数据
  {"layout":"table","title":"对比","table":{"headers":["项目","A","B"],"rows":[["耗时","3s","5s"]]}}

----- 视觉类 -----
big-number  一个关键数字放大
  {"layout":"big-number","title":"测试覆盖率","number":"87%","caption":"比上月提升 12 个百分点"}
quote  金句、名言、结论性判断
  {"layout":"quote","quote":"所有模型都是错的，但有些是有用的","author":"George Box"}
title-only  单句大字，开场观点或过渡
  {"layout":"title-only","title":"先看结论","subtitle":"后面才是推导"}
image-right  左文右图，讲概念时最合适
  {"layout":"image-right","title":"页标题","bullets":["要点"],"imagePrompt":"画面内容描述"}
image-full  整页大图压底部色带，适合章节开场
  {"layout":"image-full","title":"页标题","subtitle":"一句话","imagePrompt":"画面内容描述"}

==================== 配图 ====================
顶层可选一个封面配图：{"coverImage":"深蓝色调的抽象科技背景，简洁"}
image-right / image-full 用 imagePrompt 指定画面。

配图规则（必须遵守）：
- **绝不要写图片 URL 或本地路径** —— 模型编的链接十有八九是死的，系统也不接受
- 描述要具体到「画面里有什么」，不要写抽象概念
- 一份 deck 最多 2 张图（含封面），超出的不会生成；留给最值得配图的页
- 不要描述画面里的文字：图像模型画字必糊，需要文字就用版面自带的标题

==================== 内容纪律（直接决定成品质量）====================
1. 标题写结论，不写话题。只读标题应能听出完整故事线。
2. 一页只讲一件事。要点 3-5 条，一条不超过 30 字；超过 8 条会被打回，请拆页。
3. **绝大多数内容页都要写 takeaway** —— 没有结论条，页面就只是「罗列了一堆东西」。
4. 有数字优先用 chart / metrics / big-number，别只用文字罗列。
5. 结构建议：contents 开场 → 每章一个 section-divider → 内容页 → summary 收尾。
6. 同一个版式别连续超过 3 页，中间穿插 columns / metrics / chart / quote 更耐看。
7. 页数 6-14 页；太少显得敷衍。
8. 内容取自对方给你的材料；材料不够就照实说，不要编造数据。
9. 要分层就在二级要点前加「- 」。
10. 只写文本内容，不要写文件路径、不要写代码、不要写命令。
11. 生成后会直接把文件发到这个会话里，你不用再描述它长什么样。
"""

# 主题与版式清单都从代码生成：加了新主题/新版式不会再漏改提示词
PPTX_PROMPT_TEXT = PPTX_PROMPT_TEXT.replace("<<THEMES>>", theme_menu())
PPTX_PROMPT_TEXT = PPTX_PROMPT_TEXT.replace("<<LAYOUTS>>", " / ".join(LAYOUT_NAMES))


DECK_PROMPT_TEXT = """对方想要一份**完整的 PPT 成品**时，用这个能力直接做出来。

<<<DECK {"title": "虚拟内存", "brief": "（你自己写好的完整制作需求，见下）"}>>>

**关键纪律：brief 必须是你重新写出来的制作需求，不是把对方的话原样搬过去。**

对方通常只说一句「帮我做个关于 XX 的 PPT」，这点信息远远不够。
你要按三步加工，加工完再交出去：

1. **先理解** —— 判断对方真正要什么：什么场合用、讲给谁听、想达到什么效果。
   对方没说就按常理推断（课程作业、社团分享、工作汇报的侧重完全不同）。
2. **再补全** —— 把对方没交代、但成品必须有的东西替他定下来：
   整体结构（分几章、每章讲什么）、必须覆盖的核心内容与关键结论、
   篇幅、风格与配色倾向、要不要配图。
3. **后润色** —— 把上面这些写成一段**完整、具体、可直接执行**的需求描述，
   口吻像给专业设计师下需求，而不是像在聊天。

一段合格的 brief 应当能读到：
- 用途与场合（课堂讲解 / 求职面试 / 竞赛答辩 / 工作汇报 / 科普分享）
- 目标听众及其已有基础
- 整体结构：分几个部分、每个部分讲什么
- 必须覆盖的核心内容与关键结论
- 篇幅期望、风格与配色倾向
- 对方给过的素材要点（有的话）

对照示例 —— 对方只说「帮我做个虚拟内存的 PPT」：

✗ 不合格：「关于虚拟内存的 PPT，大学课程」
  把原话换了几个字抄一遍，下游拿不到任何有效信息。

✓ 合格：「面向大学《操作系统》课程的课堂讲解 PPT，听众是大二计算机专业学生，
  已学过进程与地址空间基础。分四部分展开：① 为什么需要虚拟内存——物理内存的
  三个局限：容量、隔离、共享；② 虚拟地址空间与页表映射机制；③ 地址转换全过程，
  含 TLB 与多级页表；④ 缺页异常处理与四种页面置换算法（OPT/LRU/FIFO/Clock）对比。
  主线是虚拟地址如何变成物理地址，结尾总结虚拟内存的三大价值。篇幅 10 页左右，
  风格简洁专业、深蓝色调，多用图示表达时序与状态转换。」

其它规则：
- title 只用于命名文件，10 字以内；brief 才是真正决定成品的东西
- 一次只做一份。对方要的是成品，不是要你先列大纲
- **信息不全就按你判断的合理默认值补齐，不要反问对方**；
  只有确实涉及对方私人的事实（姓名、学校、具体项目）才问一句
- 对方发了材料，先把要点提炼进 brief
- 生成要几分钟，期间不用解释过程
- **不要描述你是怎么做的**，不要提任何工具、平台或服务名字
- 完成后文件由系统直接发到这个会话，你不用再说「请查收」之类的话"""


READDOC_PROMPT_TEXT = """对方发来了文件，你可以读它的内容。

想读的时候用：
<<<READDOC {"index": 0}>>>

规则：
- index 表示读第几个文件：0 是最近发的那一个，往前依次是 1、2……通常用 0 就行
- 读出来的是纯文本，你据此回答；不要假装读过没读的文件
- 扫描件或纯图片 PDF 可能读不出文字，那就照实说读不出来，让对方直接贴文字
- 文件很大时只能看到前一部分，被截断会明确标出来；需要更后面的内容就问对方要
- 一次只读一个文件；要读多个就分别调用"""


# ----------------------------------------------------------------
# 公共工具
# ----------------------------------------------------------------


def _clean_text(value: Any, limit: int) -> str:
    """把模型给的任意值收成一段安全文本。

    为什么要剥控制字符：它们会让 python-docx / python-pptx 写出的
    XML 非法，报错信息还很难懂。宁可在这里静默丢掉。
    """
    text = str(value if value is not None else "")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if len(text) > limit:
        text = text[:limit]
    return text.strip()


def _safe_filename(title: str, ext: str) -> str:
    """由**系统**生成文件名。

    绝不用对方给的名字：那能带 ../、能带盘符、能带管道符。
    这里只保留中英文数字，其余一律换成下划线。
    """
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", title).strip("_")
    stem = (stem or "document")[:40]
    return stem + "_" + time.strftime("%Y%m%d_%H%M%S") + "." + ext


def _sections_of(args: dict) -> list[tuple[str, str]]:
    """把模型给的 sections 规范化成 [(heading, body)]。"""
    out: list[tuple[str, str]] = []
    for item in list(args.get("sections") or [])[:_MAX_SECTIONS]:
        if isinstance(item, dict):
            heading = _clean_text(item.get("heading"), _MAX_TITLE)
            body = _clean_text(item.get("body"), _MAX_TEXT)
        else:
            heading, body = "", _clean_text(item, _MAX_TEXT)
        if heading or body:
            out.append((heading, body))
    return out


def _slides_of(args: dict) -> list[tuple[str, list[str]]]:
    """把模型给的 slides 规范化成 [(title, [bullet])]。"""
    out: list[tuple[str, list[str]]] = []
    for item in list(args.get("slides") or [])[:_MAX_SECTIONS]:
        if not isinstance(item, dict):
            out.append(("", [_clean_text(item, 200)]))
            continue
        stitle = _clean_text(item.get("title"), _MAX_TITLE)
        bullets = [_clean_text(b, 200) for b in list(item.get("bullets") or [])[:8]]
        bullets = [b for b in bullets if b]
        if stitle or bullets:
            out.append((stitle, bullets))
    return out


# ----------------------------------------------------------------
# 文档生成
# ----------------------------------------------------------------


def _write_docx(path: Path, title: str, sections: list[tuple[str, str]]) -> None:
    """写 Word。用 python-docx，不调外部程序。"""
    from docx import Document  # 延迟导入：没装这个包时不该拖垮整个插件加载

    doc = Document()
    doc.add_heading(title, level=0)
    for heading, body in sections:
        if heading:
            doc.add_heading(heading, level=1)
        if body:
            # 按行分段，保留材料里的原始断句
            for para in body.split("\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
    doc.save(str(path))


# ----------------------------------------------------------------
# PPT 视觉主题
# ----------------------------------------------------------------

# 为什么不用 python-pptx 自带的版式：
#   默认模板是 4:3、白底、零装饰，出来就是「一页页纯文字」。
#   这里改用空白版式全部手绘：16:9 + 统一主题色 + 封面设计 + 页眉装饰 + 页码。
#   只用矩形与文本框就够撑起一套干净的学术风，不引入额外依赖。
_PPT_W = 13.333   # 英寸，16:9 宽屏
_PPT_H = 7.5

_C_PRIMARY = (0x25, 0x36, 0x5B)   # 深靛蓝：标题与主色块
_C_ACCENT = (0xE0, 0x6C, 0x4F)    # 珊瑚橙：点缀线，避免整页只有蓝色
_C_TEXT = (0x33, 0x33, 0x33)      # 正文深灰，比纯黑柔和
_C_MUTED = (0x8C, 0x8C, 0x8C)     # 页脚与页码
_C_LIGHT = (0xF2, 0xF4, 0xF8)     # 浅底块


def _rgb(triple):
    from pptx.dml.color import RGBColor

    return RGBColor(*triple)


def _set_font(run, *, size, bold=False, color=None, name="微软雅黑"):
    """统一设置字体。

    中文必须同时写 latin 与 eastasia 两个字形槽：只设 font.name 时，
    PowerPoint 中文仍会回退成宋体，整份文档立刻显旧。
    """
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = name
    if color is not None:
        run.font.color.rgb = _rgb(color)
    try:
        rpr = run._r.get_or_add_rPr()
        ea = rpr.find(qn("a:ea"))
        if ea is None:
            ea = rpr.makeelement(qn("a:ea"), {})
            rpr.append(ea)
        ea.set("typeface", name)
    except Exception:  # noqa: BLE001 — 字体细节失败不该让整页挂掉
        pass


def _rect(slide, *, left, top, width, height, color):
    """画一个无边框矩形，用作色带/装饰块。"""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.fill.background()
    # 默认的投影在这种扁平设计里只会显脏
    shape.shadow.inherit = False
    return shape


def _textbox(slide, *, left, top, width, height):
    from pptx.util import Inches

    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    return frame


def _body_size(bullets):
    """按内容量自动选字号。

    模型经常一条要点写四五十字，固定 18pt 会溢出页面。
    宁可小一点，也不要文字跑出边框。
    """
    total = sum(len(t) for t, _ in bullets)
    longest = max((len(t) for t, _ in bullets), default=0)
    if total > 320 or longest > 46:
        return 14.0
    if total > 220 or longest > 36:
        return 16.0
    return 18.0


def _body_offset(bullets, size):
    """估算正文块高度，返回一个受控的下移量（英寸）。

    中文是等宽全角字，按一个字约等于一个 font-size 估算行数即可。
    下移量封顶 1.1 英寸：再多就会把内容推到页码附近，反而难看。
    """
    per_line = max(8, int(11.5 * 72 / size))
    height = 0.0
    for text, level in bullets:
        lines = max(1, -(-len(text) // per_line))
        height += lines * (size * 1.35 + (10 if not level else 6)) / 72
    free = 4.9 - height
    if free <= 0:
        return 0.0
    # 只吃掉六成空白，让视觉重心略偏上更稳
    return min(1.1, free * 0.6)


def _write_cover(prs, title, subtitle, footer):
    """封面：左侧色带 + 大标题 + 点缀线 + 副标题 + 页脚。"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 6 = 空白版式

    _rect(slide, left=0, top=0, width=0.34, height=_PPT_H, color=_C_PRIMARY)
    _rect(slide, left=0.34, top=0, width=0.08, height=_PPT_H, color=_C_ACCENT)
    # 右下淡色块，避免右侧大面积空白显得像没做完
    _rect(slide, left=9.6, top=5.6, width=3.733, height=1.9, color=_C_LIGHT)

    frame = _textbox(slide, left=1.2, top=2.15, width=10.6, height=1.9)
    para = frame.paragraphs[0]
    run = para.add_run()
    run.text = title
    _set_font(run, size=40, bold=True, color=_C_PRIMARY)

    _rect(slide, left=1.24, top=4.18, width=1.7, height=0.06, color=_C_ACCENT)

    if subtitle:
        sub = _textbox(slide, left=1.24, top=4.42, width=10.5, height=1.2)
        run = sub.paragraphs[0].add_run()
        run.text = subtitle
        _set_font(run, size=18, color=_C_TEXT)

    if footer:
        foot = _textbox(slide, left=1.24, top=6.6, width=10.5, height=0.5)
        run = foot.paragraphs[0].add_run()
        run.text = footer
        _set_font(run, size=11, color=_C_MUTED)


def _write_content(prs, title, bullets, page):
    """内容页：顶部色条 + 标题 + 橙色点缀线 + 分级要点 + 页码。"""
    from pptx.util import Pt

    slide = prs.slides.add_slide(prs.slide_layouts[6])

    _rect(slide, left=0, top=0, width=_PPT_W, height=0.13, color=_C_PRIMARY)
    _rect(slide, left=0, top=0.13, width=2.6, height=0.05, color=_C_ACCENT)

    head = _textbox(slide, left=0.85, top=0.46, width=11.7, height=0.95)
    run = head.paragraphs[0].add_run()
    run.text = title
    _set_font(run, size=28, bold=True, color=_C_PRIMARY)

    size = _body_size(bullets)
    # 按内容量估高再居中：只有 3 条要点时，若一概贴顶排版，
    # 下半页会空掉一大块，看着像没做完。这里做一次上限受控的下移。
    top = 1.75 + _body_offset(bullets, size)
    body = _textbox(slide, left=0.95, top=top, width=11.5, height=6.6 - top)
    for index, (text, level) in enumerate(bullets):
        para = body.paragraphs[0] if index == 0 else body.add_paragraph()
        para.level = level
        # 手绘版式没有占位符的自动项目符号，前缀自己加
        marker = "– " if level else "• "
        run = para.add_run()
        run.text = marker + text
        _set_font(
            run,
            size=size if not level else max(12.0, size - 2),
            color=_C_TEXT if not level else _C_MUTED,
        )
        para.space_after = Pt(10 if not level else 6)

    page_box = _textbox(slide, left=11.9, top=6.85, width=0.95, height=0.4)
    run = page_box.paragraphs[0].add_run()
    run.text = str(page)
    _set_font(run, size=11, color=_C_MUTED)


def _write_pptx(path, title, slides, subtitle="", footer=""):
    """写一份 16:9 的 PPT：封面 + 内容页。

    bullets 里以「- 」「· 」或缩进开头的条目视为二级要点，
    这样模型按常见写法缩进就能表达层级，不必学新的参数格式。
    """
    from pptx import Presentation  # 延迟导入，理由同上
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(_PPT_W)
    prs.slide_height = Inches(_PPT_H)

    _write_cover(prs, title, subtitle, footer)

    for index, (stitle, raw_bullets) in enumerate(slides, 1):
        bullets = []
        for raw in raw_bullets:
            text = (raw or "").strip()
            level = 0
            if text.startswith(("  ", "	")) or text[:2] in ("- ", "· ", "– "):
                level = 1
                text = text.lstrip("	 -·–").strip()
            elif text.startswith("• "):
                text = text[2:].strip()
            if text:
                bullets.append((text, level))
        if not bullets:
            bullets = [("（本页无要点）", 0)]
        _write_content(prs, stitle or title, bullets, index)

    prs.save(str(path))


# ----------------------------------------------------------------
# 上传
# ----------------------------------------------------------------


async def _upload(context: Any, path: Path, name: str) -> tuple[bool, str]:
    """把生成的文件发到当前会话。

    为什么直接调 OneBot API 而不是走 reply_send：
        reply_send 收发的是**消息段**，而 OneBot v11 的上传文件是
        upload_group_file / upload_private_file 两个 API，
        没有对应的消息段类型。
    """
    bot = getattr(context, "bot", None)
    if bot is None:
        return False, "缺少 bot 上下文，无法发送文件"
    group_id = getattr(context, "group_id", "") or ""
    user_id = getattr(context, "user_id", "") or ""

    try:
        if str(group_id) and str(group_id) != "0":
            await bot.call_api(
                "upload_group_file",
                group_id=int(group_id),
                file=str(path),
                name=name,
            )
        else:
            await bot.call_api(
                "upload_private_file",
                user_id=int(user_id),
                file=str(path),
                name=name,
            )
        return True, ""
    except Exception as exc:  # noqa: BLE001 — 上传失败不该让整条回复崩掉
        return False, f"{type(exc).__name__}: {exc}"



async def _notify(context: Any, text: str) -> None:
    """长任务开始前先给一条回执。

    为什么单独发一条、而不是写进工具的返回值：
        工具的返回要等做完才回到模型，而这一步要几分钟；
        对方在这几分钟里毫无动静，会以为机器死了。
    为什么失败只记 debug 日志：
        回执发不出去不该让整个生成失败——文件才是真正的交付物。
    """
    bot = getattr(context, "bot", None)
    if bot is None or not text:
        return
    group_id = getattr(context, "group_id", "") or ""
    user_id = getattr(context, "user_id", "") or ""
    try:
        if str(group_id) and str(group_id) != "0":
            await bot.call_api("send_group_msg", group_id=int(group_id), message=text)
        elif str(user_id):
            await bot.call_api("send_private_msg", user_id=int(user_id), message=text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("deck notify failed: " + str(exc))


# ----------------------------------------------------------------
# Marker 解析（legacy 文本协议；native tool_calls 走 parameter_schema）
# ----------------------------------------------------------------


def _parse_json_marker(text: str, marker: str, tool: str):
    """解析形如 <<<MARKER {json}>>> 的标记。

    文本协议只用来兜底：结构化参数走 native tool_calls 更可靠。
    这里容错闭合符缺失，并保证 marker 不会泄漏到群里。
    """
    pattern = re.compile(r"<<<" + marker + r"\s*(.*?)\s*>>>", re.IGNORECASE | re.DOTALL)
    calls: list[ToolCall] = []
    cleaned = text or ""

    for match in pattern.finditer(text or ""):
        raw = (match.group(1) or "").strip()
        try:
            args = json.loads(raw)
        except (ValueError, TypeError):
            # JSON 不合法就当没写 —— 宁可这一轮不生成，也不要发一份空文档
            continue
        if isinstance(args, dict):
            calls.append(ToolCall(tool_name=tool, arguments=args, marker=marker))

    if calls:
        cleaned = pattern.sub("", cleaned)
    else:
        tail = re.compile(r"<<<" + marker + r"\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
        cleaned = tail.sub("", cleaned)
    return cleaned, calls


def _parse_docx(text: str):
    return _parse_json_marker(text, DOCX_MARKER, "make_docx")


def _parse_readdoc(text: str):
    return _parse_json_marker(text, READDOC_MARKER, "read_doc")


def _parse_deck(text: str):
    return _parse_json_marker(text, DECK_MARKER, "make_deck")


def _parse_pptx(text: str):
    return _parse_json_marker(text, PPTX_MARKER, "make_pptx")


# ----------------------------------------------------------------
# 执行
# ----------------------------------------------------------------


async def _resolve_deck_images(deck: dict) -> dict:
    """按 deck 里出现的图片描述生成配图，返回「描述 -> 本地路径」。

    为什么放在渲染之前异步做：render_deck 是纯同步排版，
    把网络请求塞进去会让排版和等待耦合，单张图失败也没法优雅降级。
    这里先拿到图，渲染器再按图有无决定用哪个版式。

    任何一步出问题都只返回空表 —— 配图是锦上添花，
    绝不能因为它把整份 PPT 搞垮。
    """
    from ...config import get_config
    from ...image_gen import generate_image_file

    try:
        cfg = get_config()
    except Exception:  # noqa: BLE001
        return {}
    if not getattr(cfg, "ppt_image_enable", True):
        return {}
    try:
        if not cfg.draw_configured():
            return {}
    except Exception:  # noqa: BLE001
        return {}

    prompts = image_prompts(deck)
    limit = max(1, int(getattr(cfg, "ppt_image_max", 2) or 2))
    prompts = prompts[:limit]
    if not prompts:
        return {}

    # 并发两张：再多容易被上游限流，反而更慢
    gate = asyncio.Semaphore(2)

    async def one(prompt: str):
        async with gate:
            try:
                path = await generate_image_file(prompt + IMAGE_NEGATIVE, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ppt image failed: " + str(exc))
                return prompt, None
            return prompt, path

    results = await asyncio.gather(*(one(p) for p in prompts))
    return {p: path for p, path in results if path}


async def _run(context: Any, call: ToolCall, kind: str) -> ToolResult:
    started = time.perf_counter()
    args = call.arguments or {}
    tool = call.tool_name or ("make_docx" if kind == "docx" else "make_pptx")
    title = _clean_text(args.get("title"), _MAX_TITLE) or "未命名文档"

    def done(result: ToolResult) -> ToolResult:
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    try:
        _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=f"无法创建输出目录: {exc}", error_type="mkdir_failed",
        ))

    path = _GENERATED_DIR / _safe_filename(title, kind)

    deck_note = ""
    try:
        if kind == "docx":
            sections = _sections_of(args)
            if not sections:
                return done(ToolResult(
                    tool_name=tool, tool_call_id=call.id, success=False,
                    error="没有可写入的内容（sections 为空）", error_type="empty_content",
                ))
            _write_docx(path, title, sections)
        else:
            deck = normalize_deck(args)
            # 先校验再渲染：与其交一份有问题的成品让它自己发现，
            # 不如把具体问题回给模型，让它带着修改意见重来一次（多轮打磨）。
            problems = lint_deck(deck)
            if problems:
                return done(ToolResult(
                    tool_name=tool, tool_call_id=call.id, success=False,
                    error="这份 PPT 还差点东西，改完再发一次：\n- " + "\n- ".join(problems),
                    error_type="deck_lint",
                ))
            # 页脚写死出处与日期：这是确定性事实，也省得模型每份都自己编一个
            deck["footer"] = deck.get("footer") or ("学姐 · " + time.strftime("%Y-%m-%d"))
            # 先按需生成配图（可能几十秒），再把图片交给同步渲染器
            images = await _resolve_deck_images(deck)
            warnings = render_deck(path, deck, images)
            deck_note = deck_summary(deck)
            if images:
                deck_note += "、配图 " + str(len(images)) + " 张"
            if warnings:
                deck_note += "；" + "；".join(warnings)
    except ImportError as exc:
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=f"缺少生成库: {exc}", error_type="missing_library",
        ))
    except Exception as exc:  # noqa: BLE001
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="build_failed",
        ))

    ok, err = await _upload(context, path, path.name)
    if not ok:
        # 文件已经生成好了，只是没发出去 —— 告诉模型实话，让它跟对方解释
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=f"文件已生成但发送失败: {err}", error_type="upload_failed",
            data={"path": str(path)},
        ))

    return done(ToolResult(
        tool_name=tool, tool_call_id=call.id, success=True,
        output="已生成并发送《" + title + "》" + (deck_note and ("，共 " + deck_note) or ""),
        data={"path": str(path), "name": path.name, "title": title},
    ))


async def execute_make_docx(context: Any, call: ToolCall) -> ToolResult:
    return await _run(context, call, "docx")


async def execute_make_pptx(context: Any, call: ToolCall) -> ToolResult:
    return await _run(context, call, "pptx")

async def execute_make_deck(context: Any, call: ToolCall) -> ToolResult:
    """按主题生成一份完整 PPT。

    与 make_pptx 的分工：
        make_pptx 吃「已经分好页的结构化内容」，秒级出图，走本地渲染器，
        排版完全受控但观感受版式模板限制；
        这个只吃一个主题，耗时几分钟，成品完整度和观感都好得多。
        对方只是随口要一份 PPT 时，应当走这条路径。

    异常一律转成 RETURN_TO_MODEL 的错误：生成失败要让模型知道并解释，
    而不是整条回复静默失败。
    """
    from ...doubao_ppt import DeckError, make_deck

    started = time.perf_counter()
    tool = call.tool_name or "make_deck"
    args = call.arguments or {}
    title = _clean_text(args.get("title"), _MAX_TITLE)
    brief = _clean_text(args.get("brief"), _MAX_TEXT)
    # 兼容旧字段：万一模型按老格式给，退化成用旧字段拼一份，而不是直接判失败
    if not brief:
        brief = _clean_text(args.get("requirements"), _MAX_TEXT)
        title = title or _clean_text(args.get("topic"), _MAX_TITLE)

    def done(result: ToolResult) -> ToolResult:
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    # 太短的 brief 说明模型又去当传声筒了：打回去让它重新加工，而不是直接开工。
    # 这是「用户原话不能直传」这条要求的最后一道闸。
    if len(brief) < 60:
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=("brief 太单薄，像是把对方的话原样搬过来了。请重新写："
                   "先判断对方的场合与受众，再替他把整体结构、每章要点、篇幅和风格定下来，"
                   "最后写成一段完整、具体、可直接执行的需求描述，再调用一次。"),
            error_type="brief_too_thin",
        ))

    try:
        _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error="无法创建输出目录: " + str(exc), error_type="mkdir_failed",
        ))

    # 文件名由系统生成，驱动层只负责写盘：去掉扩展名是因为底层自己会补
    title = title or brief[:20]
    stem = _safe_filename(title, "pptx")[: -len(".pptx")]

    await _notify(context, "这份我认真做，大概要几分钟，你先忙别的，好了我直接发出来。")

    try:
        path = await make_deck(brief, _GENERATED_DIR, stem)
    except DeckError as exc:
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=str(exc), error_type="deck_failed",
        ))
    except Exception as exc:  # noqa: BLE001
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error=type(exc).__name__ + ": " + str(exc), error_type="deck_failed",
        ))

    ok, err = await _upload(context, path, path.name)
    if not ok:
        return done(ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False,
            error="文件已生成但发送失败: " + err, error_type="upload_failed",
            data={"path": str(path)},
        ))

    return done(ToolResult(
        tool_name=tool, tool_call_id=call.id, success=True,
        output="已生成并发送《" + title + "》",
        data={"path": str(path), "name": path.name, "title": title},
    ))

async def execute_read_doc(context: Any, call: ToolCall) -> ToolResult:
    """读取对方本轮发来的文件内容。

    为什么从 extra 取而不接收路径参数：模型无法构造路径，
    也就无法读服务器上任意文件 —— 它只能读「本轮消息里真的带了的东西」。
    """
    from ...attachments import Attachment, clip, download_attachment, extract_text
    from .executors import _bot_of

    tool = call.tool_name or "read_doc"
    args = call.arguments or {}

    def fail(msg: str) -> ToolResult:
        return ToolResult(
            tool_name=tool, tool_call_id=call.id, success=False, output=msg,
        )

    raw_list = (getattr(context, "extra", None) or {}).get("attachments") or []
    if not raw_list:
        return fail("对方这一轮没有发文件，没有可以读的东西。")

    try:
        index = int(args.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index >= len(raw_list):
        return fail("index 超出范围：本轮共有 " + str(len(raw_list)) + " 个文件。")

    item = raw_list[index] or {}
    name = str(item.get("name") or "").strip()
    att = Attachment(
        name=name,
        size=int(item.get("size") or 0),
        file_id=str(item.get("file_id") or ""),
        busid=int(item.get("busid") or 0),
        url=str(item.get("url") or ""),
        group_id=str(item.get("group_id") or ""),
        user_id=str(item.get("user_id") or ""),
    )

    if not att.readable:
        from ...attachments import SUPPORTED_HINT

        return fail(
            "这个格式读不了：" + (name or "未知文件") + "。目前支持 " + SUPPORTED_HINT + "。"
            "可以让对方把内容直接贴出来。"
        )

    bot = _bot_of(context)
    if bot is None:
        return fail("当前环境拿不到 Bot，无法下载文件。")

    data = await download_attachment(bot, att)
    if not data:
        return fail(
            "文件没取到：" + (name or "未知文件")
            + "。可能超过 20MB 或链接已失效，可以让对方重发一次。"
        )

    text = clip(extract_text(att.name, data))
    if not text:
        return fail(
            "文件取到了但没读出文字：" + (name or "未知文件")
            + "。如果它是扫描件或图片型 PDF，那就是读不出来；"
            "可以让对方直接把文字贴出来。"
        )

    body = "【" + (name or "文件") + " 的内容】\n" + text
    return ToolResult(
        tool_name=tool, tool_call_id=call.id, success=True,
        output=body,
        data={"name": name, "chars": len(text)},
    )


# ----------------------------------------------------------------
# 可见性 / 注册
# ----------------------------------------------------------------


def _visible_read_doc(ctx: Any) -> bool:
    """只有对方真的发了文件时才可见。

    与知识库工具同一套路：条件不成立就不放进 prompt，
    既省 token，也不会让模型对着一份不存在的文件空调一轮。
    """
    return bool((getattr(ctx, "extra", None) or {}).get("attachments"))


def _visible_creator(ctx: Any) -> bool:
    """对**所有群友**可见。

    这是门一的核心诉求：普通成员也要能用它产出文档。
    安全性不靠“谁能看见”，而靠“参数里没有任何路径与命令”
    —— 见本模块开头的设计说明。
    """
    return True


def _visible_deck(ctx: Any) -> bool:
    """与其它生成工具同一套可见性，另加一道应急开关。

    为什么单独判一次：这条路径依赖本机常驻进程，卡住会占住串行队列。
    真出问题时希望是「改配置停用」而不是「改代码重新部署」。
    配置读不到时按可用处理 —— 宁可多一个工具，也不要因为配置异常整层消失。
    """
    try:
        from ...config import get_config

        return bool(getattr(get_config(), "deck_enable", True))
    except Exception:  # noqa: BLE001
        return True


def build_creator_tools() -> list[ToolDefinition]:
    """构造生成层工具。

    Continuation = SIDE_EFFECT_ONLY：文件已经发出去了，不需要再回模型一轮。
    FailurePolicy = RETURN_TO_MODEL：生成/发送失败要让模型知道，
    由它决定怎么解释，而不是整条回复失败。
    """
    docx_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "文档标题"},
            "sections": {
                "type": "array",
                "description": "小节列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string", "description": "小节标题"},
                        "body": {"type": "string", "description": "小节正文"},
                    },
                },
            },
        },
        "required": ["title", "sections"],
    }
    pptx_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "演示标题（封面大字）"},
            "subtitle": {
                "type": "string",
                "description": "封面副标题，如课程名或一句话概括",
            },
            "theme": {
                "type": "string",
                "enum": list(THEMES.keys()),
                "description": "配色主题，按内容场景选：" + " / ".join(
                    k + " " + v["label"] for k, v in THEMES.items()
                ),
            },
            "coverImage": {
                "type": "string",
                "description": "封面配图描述（画面内容，不是 URL）",
            },
            "footer": {
                "type": "string",
                "description": "页脚出处（书名/单位/课程名），会出现在每页左下角",
            },
            "slides": {
                "type": "array",
                "description": "内容页列表；每页必须给 layout，可选 "
                + " / ".join(LAYOUT_NAMES),
                "items": {
                    "type": "object",
                    "properties": {
                        "layout": {
                            "type": "string",
                            "enum": LAYOUT_NAMES,
                            "description": "这一页的版式，系统按它排版",
                        },
                        "title": {"type": "string", "description": "页标题（结论式短语）"},
                        "subtitle": {"type": "string", "description": "副标题（section-divider / title-only 用）"},
                        "chapter": {"type": "string", "description": "章节码，两位数字，如 01"},
                        "section": {"type": "string", "description": "小节名，2-8 字"},
                        "scope": {"type": "string", "description": "本页范围/怎么读，一行 10-30 字"},
                        "takeaway": {"type": "string", "description": "底部收束条：一句话 so-what"},
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要点；前缀加「- 」表示二级要点",
                        },
                        "left": {
                            "type": "object",
                            "description": "two-column 左栏",
                            "properties": {
                                "heading": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "right": {
                            "type": "object",
                            "description": "two-column 右栏",
                            "properties": {
                                "heading": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "imagePrompt": {
                            "type": "string",
                            "description": "image-right / image-full 的画面描述（不是 URL）",
                        },
                        "columns": {
                            "type": "array",
                            "description": "columns 版式：2-4 栏并列论证",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {"type": "string"},
                                    "body": {"type": "string", "description": "一段短说明"},
                                    "bullets": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "metrics": {
                            "type": "array",
                            "description": "metrics 版式：3-6 个指标卡",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {"type": "string", "description": "大数字，如 98.2%"},
                                    "label": {"type": "string", "description": "指标名"},
                                    "note": {"type": "string", "description": "口径或说明"},
                                },
                            },
                        },
                        "matrix": {
                            "type": "object",
                            "description": "matrix 版式：2x2 矩阵",
                            "properties": {
                                "xLabel": {"type": "string"},
                                "yLabel": {"type": "string"},
                                "quadrants": {
                                    "type": "array",
                                    "description": "正好 4 个，顺序：左上、右上、左下、右下",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string"},
                                            "desc": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                        "nodes": {
                            "type": "array",
                            "description": "timeline 版式：2-6 个节点",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "string"},
                                    "title": {"type": "string"},
                                    "desc": {"type": "string"},
                                },
                            },
                        },
                        "steps": {
                            "type": "array",
                            "description": "process 版式：2-5 个步骤",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "desc": {"type": "string"},
                                },
                            },
                        },
                        "items": {
                            "type": "array",
                            "description": "contents 版式：每个章节一行",
                            "items": {"type": "string"},
                        },
                        "number": {"type": "string", "description": "big-number 的大数字，如 87%"},
                        "caption": {"type": "string", "description": "big-number 的说明文字"},
                        "quote": {"type": "string", "description": "quote 的引用内容"},
                        "author": {"type": "string", "description": "quote 的出处"},
                        "chart": {
                            "type": "object",
                            "description": "chart 版式的数据",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["bar", "barh", "line", "pie"],
                                },
                                "categories": {"type": "array", "items": {"type": "string"}},
                                "series": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "values": {
                                                "type": "array",
                                                "items": {"type": "number"},
                                            },
                                        },
                                    },
                                },
                                "takeaway": {"type": "string", "description": "一句话结论"},
                            },
                        },
                        "table": {
                            "type": "object",
                            "description": "table 版式的数据",
                            "properties": {
                                "headers": {"type": "array", "items": {"type": "string"}},
                                "rows": {
                                    "type": "array",
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "required": ["layout"],
                },
            },
        },
        "required": ["title", "slides"],
    }
    readdoc_schema = {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "读第几个文件：0 是最新发来的那一个，往前依次 1、2……",
            },
        },
    }
    deck_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "PPT 标题，仅用于命名文件，10 字以内",
            },
            "brief": {
                "type": "string",
                "description": (
                    "你加工好的完整制作需求（理解 + 补全 + 润色之后的），"
                    "不是对方原话的复述。要写清用途场合、目标听众、整体结构、"
                    "每部分讲什么、必须覆盖的核心内容与结论、篇幅、风格配色。"
                    "这份内容直接决定成品质量，务必写满"
                ),
            },
        },
        "required": ["title", "brief"],
    }
    return [
        ToolDefinition(
            name="make_docx",
            description="把材料整理成一份 Word 文档（.docx）并发给对方",
            marker=DOCX_MARKER,
            prompt_text=DOCX_PROMPT_TEXT,
            parser=_parse_docx,
            executor=execute_make_docx,
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            permission="creator.docx",
            visibility=_visible_creator,
            side_effect=True,
            owner=CREATOR_OWNER,
            priority=46,
            timeout=30.0,
            parameter_schema=docx_schema,
        ),
        ToolDefinition(
            name="make_pptx",
            description="把材料整理成一份 PPT（.pptx）并发给对方",
            marker=PPTX_MARKER,
            prompt_text=PPTX_PROMPT_TEXT,
            parser=_parse_pptx,
            executor=execute_make_pptx,
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            permission="creator.pptx",
            visibility=_visible_creator,
            side_effect=True,
            owner=CREATOR_OWNER,
            priority=46,
            # 要等配图生成，30 秒远远不够
            timeout=300.0,
            parameter_schema=pptx_schema,
        ),
        ToolDefinition(
            name="read_doc",
            description="读取对方发来的文件内容（pdf/docx/pptx/xlsx/文本等）",
            marker=READDOC_MARKER,
            prompt_text=READDOC_PROMPT_TEXT,
            parser=_parse_readdoc,
            executor=execute_read_doc,
            # 要把文件文本交回模型，所以是 RETURN_TO_MODEL 而不是 SIDE_EFFECT_ONLY
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            continuation=Continuation.RETURN_TO_MODEL,
            permission="creator.readdoc",
            visibility=_visible_read_doc,
            side_effect=False,
            owner=CREATOR_OWNER,
            priority=45,
            # 下载 + 解析大 PDF 可能慢，给足时间
            timeout=60.0,
            parameter_schema=readdoc_schema,
        ),
        ToolDefinition(
            name="make_deck",
            description="按主题做一份完整的 PPT（.pptx）并发给对方",
            marker=DECK_MARKER,
            prompt_text=DECK_PROMPT_TEXT,
            parser=_parse_deck,
            executor=execute_make_deck,
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            permission="creator.deck",
            # 所有群友都能用；另有一道配置开关可整体停用
            visibility=_visible_deck,
            side_effect=True,
            owner=CREATOR_OWNER,
            # 比 make_pptx 高一级：对方随口要 PPT 时优先走成品质量更好的这条
            priority=47,
            # 实测一份约 5 分钟，留足余量；超时只是这一轮失败，不会拖垮其他会话
            timeout=900.0,
            parameter_schema=deck_schema,
        ),
    ]


def register_creator_tools(registry: Any) -> list[str]:
    """把生成层工具注册进工具表（幂等）。

    为什么不塞进 register_production_tools：
        那个函数的语义是“8 个生产工具”，被约 20 个测试直接依赖。
        生成层是新增能力，混进去会让那些断言失去意义。

    幂等性：ToolRegistry.register 按 name 覆盖，重复调用不会重复项。
    """
    names: list[str] = []
    for tool in build_creator_tools():
        registry.register(tool)
        names.append(tool.name)
    return names


__all__ = [
    "CREATOR_OWNER",
    "DOCX_MARKER",
    "PPTX_MARKER",
    "DECK_MARKER",
    "READDOC_MARKER",
    "build_creator_tools",
    "execute_make_deck",
    "execute_make_docx",
    "execute_make_pptx",
    "execute_read_doc",
    "register_creator_tools",
]
