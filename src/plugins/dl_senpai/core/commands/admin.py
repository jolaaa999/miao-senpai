"""管理员「喵指令」在 Command Registry 里的登记。

为什么单独一个文件而不是塞进 builtin.py：

- builtin.py 里的六条是**群友的玩法**（签到/商店/好感度/认证），
  它们共用一套 guard（群白名单 + 功能开关）；
- 这一批是**管理员的管理动作**，guard 完全不同（只认管理员 QQ），
  priority 也不同（必须比普通命令先匹配，否则「喵名片」可能被别的规则截走）。

放一起会让两种语义混在一张表里，日后想给管理指令加东西就得在同一份
列表里挑一个不打架的数字——这正是 Phase 3 之前 matcher priority 的旧问题。

指令的实现都在 admin_commands.py；这里只做「声明」：
名字、别名、说明、优先级、是否短路。新增一条管理指令 = 下面加一项。
"""

from __future__ import annotations

from typing import Any, Sequence

from ...observability.logging import log_event
from .models import Command
from .registry import PERMISSION_ADMIN, CommandRegistry

# 管理指令的命名空间。喵帮助按它筛指令，控制台也可以按它整组展示。
NS_ADMIN = "admin"

# 优先级：比普通命令（50）小 = 先匹配。
# 但比认证 gate（10）大：新人认证是「会话门」，不该被管理指令插队。
PRIORITY_ADMIN = 30

# 指令别名表：action -> (中文/英文别名…)。
# 多写几个英文别名是为了让从别的 bot 迁过来的人不用改肌肉记忆
# （/silence、#sl 这类写法在 HoshinoBot / AstrBot 生态里很常见）。
# 格式约定（2026-10-28 定）：**中文指令统一写「喵/指令」**，例如 喵/禁言、喵/撤回。
# 旧的无斜杠写法（喵禁言）与市面写法（/silence、#sl、!禁言）继续兼容 ——
# 「喵/」在 registry 里会被归一化成「/」，所以中文命令走同一条正则。
# 为什么以斜杠为主：中文指令不加分隔符时，指令名和语气词连在一起（喵禁言/喵禁言语），
# 边界只能靠正则兜；加一个斜杠等于把「这是给指令的」明写出来。

ALIASES: dict[str, tuple[str, ...]] = {
    "mute": ("禁言", "口球", "小黑屋", "silence", "mute", "sl", "ban"),
    "unmute": ("解禁", "解除禁言", "放人", "unmute", "unsilence"),
    "muteall": ("全员禁言", "全体禁言", "全禁", "muteall"),
    "kick": ("踢", "踢人", "踢出去", "kick"),
    "recall": ("撤回", "撤掉", "撤销", "recall", "revoke"),
    "card": ("名片", "改名片", "card"),
    "title": ("头衔", "称号", "title"),
    "setadmin": ("设管", "上管", "给管理", "setadmin"),
    "unsetadmin": ("撤管", "下管", "取消管理", "unsetadmin"),
    "notice": ("公告", "群公告", "announce", "notice"),
    "silence": ("闭嘴", "安静", "静默", "sleeptalk", "hush"),
    "wake": ("醒", "醒来", "恢复说话", "wake"),
    "echo": ("复读", "跟着说", "echo", "say"),
    "feature": ("功能", "开关", "能力", "feature"),
    "status": ("状态", "自检", "status", "state"),
    "help": ("帮助", "菜单", "指令表", "help", "commands"),
    "ping": ("ping", "在吗", "活着吗"),
}

# 说明文案会直接出现在「喵帮助」里，所以写成「用法 — 效果」的形状，
# 而不是给开发者看的短描述。
# 说明文案会直接出现在「喵help」里，格式统一为：
#     用法 — 说明
# 用法第一段必须是指令名（英文，和别名表里的英文别名一致），help 会按空格
# 切成「指令 / 参数」两列对齐，所以这里多一个空格都可能让整列错位。
# 为什么改成英文名（2026-10-28）：中文指令必须带分隔符（喵/禁言）才能定边界，
# 而英文可以直连写（喵help、喵mute），少一个字符、少一次切换输入法。
# 中文别名没有删，仍然可用，只是不进帮助。
DESCRIPTIONS: dict[str, str] = {
    "mute": "mute @某人 [10m] — 禁言，默认 60 秒（10m / 1h / 1d）",
    "unmute": "unmute @某人 — 提前解除禁言",
    "muteall": "muteall [on|off] — 全群禁言，默认开",
    "kick": "kick @某人 [拉黑] — 踢出本群，加「拉黑」则不许再加回来",
    "recall": "recall — 撤回被引用的那条，也可直接给消息 id",
    "recallrecent": "recallrecent [N] — 撤掉她自己最近 N 条（默认 3，最多 20）",
    "card": "card @某人 [新名片] — 改群名片，不给内容则清空",
    "title": "title @某人 [头衔] — 设专属头衔，需要群主身份",
    "setadmin": "setadmin @某人 — 设为群管理员",
    "unsetadmin": "unsetadmin @某人 — 取消群管理员",
    "notice": "notice 正文 — 发群公告",
    "noticedel": "noticedel [序号] — 删群公告，默认最新那条",
    "leave": "leave 确认 — 退出本群，必须带「确认」",
    "whois": "whois @某人 — 角色、入群时间、名片、是否在禁言里",
    "members": "members [页] — 成员列表，一页 20 人",
    "admins": "admins — 群主与管理员",
    "mutelist": "mutelist — 谁在禁言里、各剩多久",
    "groupinfo": "groupinfo — 群名、人数上限、建群时间",
    "notices": "notices — 群公告列表",
    "files": "files — 群文件",
    "essence": "essence — 群精华",
    "albums": "albums — 群相册",
    "warn": "warn @某人 [原因] — 记一次警告，满 3 次自动禁言 10 分钟",
    "warnlist": "warnlist — 谁被警告过、各有几次",
    "warnclear": "warnclear [@某人] — 清警告，不带人则清空本群",
    "blacklist": "blacklist @某人 [原因] — 她不再接这个人的话",
    "unblacklist": "unblacklist @某人 — 恢复接话",
    "blacklistlist": "blacklistlist — 本群被拉黑的人",
    "silence": "silence [分钟] — 本群静默，期间对谁都不回（默认 30 分钟）",
    "wake": "wake — 解除本群静默",
    "echo": "echo 文本 — 让她原样说一句，不经过 AI",
    "feature": "feature [功能 on|off] — 按群开关能力，不带参数列清单",
    "status": "status — 在线时长、指令数、管理员数、本群状态",
    "help": "help [组名|关键词] — 这张表",
    "ping": "ping — 她在不在",
}


# 「喵/帮助」的分组：组名 -> action（有序，顺序就是帮助里的顺序）。
# 为什么要在注册表之外再维护一张分组表：注册表只保证「能匹配」，
# 46 条指令平铺出来是一堵墙，管理员扫不出自己要找的那条。
# 新增指令必须在这张表里出现，否则有测试会失败（help 里不许漏指令）。
GROUPS: dict[str, tuple[str, ...]] = {
    "群管理": (
        "kick", "mute", "unmute", "muteall", "recall", "recallrecent",
        "card", "title", "setadmin", "unsetadmin", "notice", "noticedel", "leave",
    ),
    "群信息": (
        "whois", "members", "admins", "mutelist", "groupinfo",
        "notices", "files", "essence", "albums",
    ),
    "风控": ("warn", "warnlist", "warnclear", "bad", "blacklist", "unblacklist", "blacklistlist"),
    "学姐自身": ("silence", "wake", "echo", "feature", "status", "help", "ping"),
    "运维": ("memory", "forget", "perm", "health", "reload", "log"),
    "小工具": ("dice", "lots", "calc", "wordbank", "timer"),
}



#: 所有人可用的玩法指令：(触发词, 说明)。**这里不带「喵」**——
# 带喵就是主人专属，是两条互不相通的路（见 registry.is_admin_prefixed）。
# 为什么要在这张表里手写触发词：玩法指令的正则散在 checkin/shop/affection/
# group_verify 各自的模块里，从正则反推「人话触发词」既不可靠也不可读。
# 代价是可能和实现脱节，所以有测试逐条拿触发词去注册表里跑一遍。
ALL_LEVEL: tuple[tuple[str, str], ...] = (
    ("签到 / 打卡", "每日签到拿积分，连续签到有加成"),
    ("我的积分 / 积分排行", "看自己有多少积分、群里谁最多"),
    ("今日任务 / 完成任务", "每日任务的进度与领取"),
    ("商店 / 积分商店", "用积分给学姐挑衣服"),
    ("购买 X / 换上 X", "买下某件衣服 / 从仓库里换装"),
    ("仓库 / 衣橱", "看自己买过哪些衣服"),
    ("好感度 / 好感排行", "看学姐对你的好感度、群里排行"),
    ("抽签 / 运势", "今日签文，可带事项：抽签 今天面试"),
    ("骰子 / 掷骰", "掷骰子，默认 1d6：骰子 2d6"),
    ("计算 1+1 / 算一下 1+1", "算数，不走模型，结果确定"),
    ("我的信息 / 我是谁", "自己的名片、身份、等级、入群时间"),
    ("群信息 / 本群信息", "群名、人数上限、建群时间"),
    ("群公告 / 看公告", "本群公告列表"),
    ("群文件 / 本群文件", "群里上传过的文件"),
    ("禁言名单 / 谁被禁言了", "现在谁在禁言里、各剩多久"),
    ("管理名单 / 本群管理", "群主和管理员是谁"),
    ("我的警告 / 我的违规", "自己被警告了几次、为什么"),
    ("改名片 新名字", "改自己的群名片"),
    ("认证", "新人进群后的认证（群里开了门禁才有）"),
)



def merged_tables() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """把扩展模块（admin_commands_extra）的别名与说明合并进来。

    为什么要懒加载：扩展模块要从 admin_commands 导入工具函数，在顶层导入
    会形成循环。合并放在调用时做，注册只发生一次，开销可以忽略。
    """
    from ...admin_commands_extra import ALIASES_EXTRA, DESCRIPTIONS_EXTRA

    aliases = {**ALIASES, **ALIASES_EXTRA}
    descriptions = {**DESCRIPTIONS, **DESCRIPTIONS_EXTRA}
    return aliases, descriptions


def build_admin_commands() -> list[Command]:
    """生成管理指令定义列表（不注册）。

    所有指令共用同一个 handler：分派逻辑在 admin_commands.handle_admin_command 里
    按 match.action 走，这样「一条指令一个函数」的写法不会在 registry 里
    膨胀成十几个几乎一样的东西。
    """
    from ...admin_commands import build_pattern, handle_admin_command

    aliases_table, descriptions = merged_tables()
    commands: list[Command] = []
    for action, aliases in aliases_table.items():
        commands.append(
            Command(
                name=f"admin_{action}",
                namespace=NS_ADMIN,
                patterns=[(action, build_pattern(aliases))],
                handler=handle_admin_command,
                description=descriptions.get(action, action),
                priority=PRIORITY_ADMIN,
                # 管理员专属标记：registry 靠它实现「喵开头的输入只走管理指令」，
                # 所以「喵/签到」永远不会被当成玩法指令执行。
                permission=PERMISSION_ADMIN,
                # help 是唯一例外：群友发「喵help」也得有回应，
                # 只是内容按身份分叉（群友只看得到 ALL_LEVEL 那些）。
                open_admin_prefix=(action == "help"),
                # 命中就短路：管理指令处理完绝不能让 AI 再补一句，
                # 否则「踢人」后面跟一句撒娇，场面很怪。
                consume=True,
                owner="admin_commands",
            )
        )
    return commands


def register_admin_commands(
    registry: CommandRegistry, owner: str = "admin_commands"
) -> list[Command]:
    """注册全部管理指令。owner 用于整组停用（PluginRuntime.disable）。"""
    commands = build_admin_commands()
    for cmd in commands:
        if owner:
            cmd.owner = owner
        registry.register(cmd)
    log_event(
        "admin_commands.ready",
        count=len(commands),
        actions=sorted(merged_tables()[0]),
        owner=owner,
    )
    return commands


__all__ = [
    "ALIASES",
    "ALL_LEVEL",
    "GROUPS",
    "DESCRIPTIONS",
    "NS_ADMIN",
    "PRIORITY_ADMIN",
    "build_admin_commands",
    "register_admin_commands",
]
