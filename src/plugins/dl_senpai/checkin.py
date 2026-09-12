from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=8))

CheckinCmd = Literal[
    "checkin",
    "status",
    "leaderboard_points",
    "leaderboard_streak",
    "task_done",
    "task_view",
    "help",
    "none",
]

TITLE_MILESTONES: list[tuple[int, str]] = [
    (3, "打卡达人"),
    (7, "一周卷王候补"),
    (15, "续命高手"),
    (30, "学姐认证钉子户"),
]

FORTUNES = (
    "今日宜写代码，忌空跑训练～",
    "今日宜问问题，学姐在线答疑哦。",
    "今日宜复盘一道题，别只收藏不练。",
    "今日宜早点睡，显卡和脑子都需要散热。",
    "今日宜整理笔记，明天的你会感谢自己。",
    "今日宜大胆提问，装懂才是真危险。",
    "今日宜小步提交，忌一口气重构宇宙。",
    "今日宜喝水摸鱼交替进行，劳逸结合～",
)

TASK_POOL = (
    # —— 学习向（少量）——
    ("ask_algo", "今天去 @学姐 问一道算法/刷题相关的小问题", 8),
    ("ask_debug", "今天跟学姐吐槽/请教一个 bug 或报错", 8),
    ("share_note", "今天在群里分享一句你最近学到的小知识", 6),
    ("read_docs", "今天认真读一段文档/题解，回来跟学姐说你读了啥", 7),
    # —— 群互动 / 整活 ——
    ("meow_three", "在群里学猫叫三声：连发「喵喵喵」（学姐要听见哦）", 5),
    ("call_senpai", "正经 @学姐 喊一声「学姐好」，别害羞", 4),
    ("send_sticker", "给学姐发一个表情包（或回复学姐消息再发表情）", 5),
    ("encourage", "今天给任意群友发一句真心打气的话（别阴阳）", 5),
    ("praise_senpai", "夸学姐一句（可以很敷衍，但要夸）", 4),
    ("weather_chat", "跟学姐聊一句今天天气/心情，哪怕只说「好热」也行", 4),
    ("good_night", "今晚在群里说一句「晚安」或「早睡」（学姐记着查岗）", 4),
    ("water_reminder", "现在立刻去喝一口水，回来跟学姐汇报「喝了」", 3),
    ("stretch", "站起来伸个懒腰/转转脖子，回来打卡说「活动过了」", 3),
    ("meme_share", "在群里丢一个你最近觉得好笑的梗/表情（别引战）", 5),
    ("thank_someone", "谢谢一位今天帮过你的人（群友/同学/家人都行），并说出来", 5),
    ("introduce_food", "跟学姐安利一道你最近爱吃的（外卖也算）", 4),
    ("song_line", "哼一句歌词或歌名出来（打字也行）", 4),
    ("emoji_story", "只用 3～5 个 emoji 讲一件小事，让学姐猜猜你在干嘛", 5),
    ("group_hi", "跟群里至少一位同学打招呼（点名也行）", 4),
    ("selfie_desc", "用一句话形容你此刻的桌面/房间状态（乱也没关系）", 4),
    ("wish_luck", "给明天的自己写一句加油/提醒，发在群里", 4),
    ("pet_talk", "如果有宠物就摸摸它；没有就对空气说「摸摸学姐脑袋」（字面打出来）", 5),
)


@dataclass
class PendingTask:
    id: str
    text: str
    reward: int
    assigned_date: str  # YYYY-MM-DD


@dataclass
class UserCheckin:
    user_id: str
    display_name: str = ""
    points: int = 0
    streak: int = 0
    max_streak: int = 0
    last_checkin: str = ""  # YYYY-MM-DD
    total_checkins: int = 0
    title: str = ""
    pending_task: PendingTask | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.pending_task is None:
            d["pending_task"] = None
        return d

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any]) -> UserCheckin:
        task_raw = data.get("pending_task")
        task = None
        if isinstance(task_raw, dict) and task_raw.get("id") and task_raw.get("text"):
            task = PendingTask(
                id=str(task_raw["id"]),
                text=str(task_raw["text"]),
                reward=int(task_raw.get("reward") or 5),
                assigned_date=str(task_raw.get("assigned_date") or ""),
            )
        return cls(
            user_id=str(user_id),
            display_name=str(data.get("display_name") or ""),
            points=int(data.get("points") or 0),
            streak=int(data.get("streak") or 0),
            max_streak=int(data.get("max_streak") or 0),
            last_checkin=str(data.get("last_checkin") or ""),
            total_checkins=int(data.get("total_checkins") or 0),
            title=str(data.get("title") or ""),
            pending_task=task,
        )


@dataclass
class CheckinResult:
    ok: bool
    already: bool = False
    streak_broken: bool = False
    points_gained: int = 0
    streak: int = 0
    max_streak: int = 0
    total_points: int = 0
    title: str = ""
    title_upgraded: bool = False
    fortune: str = ""
    task: PendingTask | None = None
    user: UserCheckin | None = None
    message: str = ""


def today_str(now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    return now.astimezone(TZ).date().isoformat()


def parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def title_for_streak(streak: int) -> str:
    title = ""
    for need, name in TITLE_MILESTONES:
        if streak >= need:
            title = name
    return title


def base_checkin_points(streak: int) -> int:
    # 基础 10 + 连续加成（最多 +7）
    return 10 + min(max(streak, 1), 7)


class CheckinStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path(self, group_id: int | str) -> Path:
        return self.data_dir / f"group_{group_id}.json"

    def load_group(self, group_id: int | str) -> dict[str, UserCheckin]:
        path = self._path(group_id)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        users_raw = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(users_raw, dict):
            return {}
        out: dict[str, UserCheckin] = {}
        for uid, row in users_raw.items():
            if isinstance(row, dict):
                out[str(uid)] = UserCheckin.from_dict(str(uid), row)
        return out

    def save_group(self, group_id: int | str, users: dict[str, UserCheckin]) -> None:
        path = self._path(group_id)
        payload = {
            "users": {uid: u.to_dict() for uid, u in users.items()},
            "updated_at": datetime.now(TZ).isoformat(),
        }
        with self._lock:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get_user(self, group_id: int | str, user_id: int | str) -> UserCheckin:
        users = self.load_group(group_id)
        uid = str(user_id)
        return users.get(uid) or UserCheckin(user_id=uid)

    def checkin(
        self,
        group_id: int | str,
        user_id: int | str,
        *,
        display_name: str = "",
        now: datetime | None = None,
        rng: random.Random | None = None,
    ) -> CheckinResult:
        rng = rng or random.Random()
        now = now or datetime.now(TZ)
        today = today_str(now)
        users = self.load_group(group_id)
        uid = str(user_id)
        user = users.get(uid) or UserCheckin(user_id=uid)
        if display_name:
            user.display_name = display_name

        last = parse_date(user.last_checkin)
        today_d = now.astimezone(TZ).date()

        if last == today_d:
            return CheckinResult(
                ok=False,
                already=True,
                streak=user.streak,
                max_streak=user.max_streak,
                total_points=user.points,
                title=user.title,
                user=user,
                message="今天已经签过啦，别刷屏～明天再来。",
            )

        streak_broken = False
        if last is None:
            user.streak = 1
        elif last == today_d - timedelta(days=1):
            user.streak += 1
        else:
            streak_broken = True
            user.streak = 1  # 断签清零，从 1 重新计

        user.last_checkin = today
        user.total_checkins += 1
        user.max_streak = max(user.max_streak, user.streak)

        gained = base_checkin_points(user.streak)
        user.points += gained

        old_title = user.title
        new_title = title_for_streak(user.streak)
        title_upgraded = bool(new_title) and new_title != old_title and (
            not old_title
            or next((n for n, t in TITLE_MILESTONES if t == new_title), 0)
            > next((n for n, t in TITLE_MILESTONES if t == old_title), 0)
        )
        if new_title:
            user.title = new_title

        # 过期旧任务
        if user.pending_task and user.pending_task.assigned_date != today:
            user.pending_task = None

        task = None
        if user.pending_task is None:
            tid, text, reward = rng.choice(TASK_POOL)
            task = PendingTask(
                id=f"{tid}_{today}",
                text=text,
                reward=reward,
                assigned_date=today,
            )
            user.pending_task = task

        fortune = rng.choice(FORTUNES)
        users[uid] = user
        self.save_group(group_id, users)

        return CheckinResult(
            ok=True,
            streak_broken=streak_broken,
            points_gained=gained,
            streak=user.streak,
            max_streak=user.max_streak,
            total_points=user.points,
            title=user.title,
            title_upgraded=title_upgraded,
            fortune=fortune,
            task=user.pending_task,
            user=user,
        )

    def complete_task(
        self,
        group_id: int | str,
        user_id: int | str,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str, UserCheckin]:
        now = now or datetime.now(TZ)
        today = today_str(now)
        users = self.load_group(group_id)
        uid = str(user_id)
        user = users.get(uid) or UserCheckin(user_id=uid)
        task = user.pending_task
        if task is None:
            return False, "你现在没有待完成的签到任务哦，先「签到」抽一个～", user
        if task.assigned_date and task.assigned_date != today:
            user.pending_task = None
            users[uid] = user
            self.save_group(group_id, users)
            return False, "昨天的任务过期啦，重新签到再抽一张吧。", user
        reward = int(task.reward)
        user.points += reward
        done_text = task.text
        user.pending_task = None
        users[uid] = user
        self.save_group(group_id, users)
        return (
            True,
            format_task_done(
                name=user.display_name or "你",
                done_text=done_text,
                reward=reward,
                total_points=user.points,
            ),
            user,
        )

    def leaderboard(
        self,
        group_id: int | str,
        *,
        by: Literal["points", "streak"] = "points",
        limit: int = 10,
    ) -> list[UserCheckin]:
        users = list(self.load_group(group_id).values())
        if by == "streak":
            users.sort(key=lambda u: (u.streak, u.points, u.total_checkins), reverse=True)
        else:
            users.sort(key=lambda u: (u.points, u.streak, u.total_checkins), reverse=True)
        return users[: max(1, limit)]

    def spend_points(
        self,
        group_id: int | str,
        user_id: int | str,
        amount: int,
    ) -> tuple[bool, str, UserCheckin]:
        if amount <= 0:
            user = self.get_user(group_id, user_id)
            return False, "积分数量不对哦～", user
        users = self.load_group(group_id)
        uid = str(user_id)
        user = users.get(uid) or UserCheckin(user_id=uid)
        if user.points < amount:
            short = amount - user.points
            return (
                False,
                f"积分不够啦，还差 {short} 分～多签到做任务攒一攒？",
                user,
            )
        user.points -= amount
        users[uid] = user
        self.save_group(group_id, users)
        return True, "", user


_CMD_PATTERNS: list[tuple[CheckinCmd, re.Pattern[str]]] = [
    ("help", re.compile(r"^(签到帮助|签到说明|打卡帮助)$")),
    ("checkin", re.compile(r"^(签到|打卡)$")),
    ("status", re.compile(r"^(我的积分|签到状态|我的签到|签到查询)$")),
    ("leaderboard_points", re.compile(r"^(积分排行|积分榜|签到排行|排行榜)$")),
    ("leaderboard_streak", re.compile(r"^(连续排行|连续榜|连签排行)$")),
    ("task_done", re.compile(r"^(完成任务|交作业|任务完成)$")),
    ("task_view", re.compile(r"^(今日任务|我的任务|查看任务)$")),
]


def parse_checkin_command(text: str) -> CheckinCmd:
    t = (text or "").strip()
    # 去掉对机器人的纯 @ 残留空白后匹配整句
    t = re.sub(r"\s+", "", t)
    if not t:
        return "none"
    for cmd, pat in _CMD_PATTERNS:
        if pat.match(t):
            return cmd
    return "none"


_CHECKIN_RULE = "────────"


def format_checkin_reply(result: CheckinResult, *, name: str) -> str:
    """按「签到小卡片」规范排版。"""
    if result.already:
        return (
            f"📌 今天已经签过啦\n"
            f"{name}，{result.message}\n"
            f"想看战绩就发「我的积分」～"
        )

    if result.streak_broken:
        headline = f"{name} 回来啦……断签了呢，哼。"
        blurb = f"连续重计为 {result.streak} 天，本次还是给你 +{result.points_gained} 分。"
    elif result.streak == 1:
        headline = f"{name} 签到成功～"
        blurb = f"新的连续从今天开始，+{result.points_gained} 分。"
    elif result.streak >= 7:
        headline = f"{name} 连续 {result.streak} 天？有点东西。"
        blurb = f"行吧，学姐承认你挺能坚持的。+{result.points_gained} 分。"
    else:
        headline = f"{name} 签到成功！"
        blurb = f"连续 {result.streak} 天，+{result.points_gained} 分，继续保持哦～"

    lines = [
        "📌 签到成功",
        headline,
        blurb,
        _CHECKIN_RULE,
        f"连续 {result.streak} 天 · 本次 +{result.points_gained} 分",
        f"积分 {result.total_points} · 最长连签 {result.max_streak} 天",
    ]
    if result.title:
        if result.title_upgraded:
            lines.append(f"🎖️ 新称号！「{result.title}」（学姐会帮你挂群头衔哦）")
        else:
            lines.append(f"🎖️ 称号：「{result.title}」")
    if result.fortune:
        lines.append(f"✨ 寄语：{result.fortune}")
    if result.task:
        lines.append(_CHECKIN_RULE)
        lines.append("今日任务")
        lines.append(result.task.text)
        lines.append(f"完成后发「完成任务」· +{result.task.reward} 分")
    return "\n".join(lines)


def format_status(user: UserCheckin, *, name: str) -> str:
    title = user.title or "暂无"
    lines = [
        "📋 签到档案",
        f"{name}",
        _CHECKIN_RULE,
        f"积分 {user.points} · 连续 {user.streak} 天",
        f"最长 {user.max_streak} 天 · 累计 {user.total_checkins} 次",
        f"🎖️ 称号：{title}",
        f"上次签到：{user.last_checkin or '还没签过'}",
    ]
    if user.pending_task:
        lines.append(_CHECKIN_RULE)
        lines.append("今日任务")
        lines.append(user.pending_task.text)
        lines.append(f"完成后发「完成任务」· +{user.pending_task.reward} 分")
    else:
        lines.append("今日任务：无（先「签到」抽一张）")
    return "\n".join(lines)


def format_leaderboard(
    users: list[UserCheckin],
    *,
    by: Literal["points", "streak"],
) -> str:
    if not users:
        return "🏆 排行榜还是空的\n快来「签到」当第一名～"
    title = "积分排行榜" if by == "points" else "连续签到排行榜"
    lines = [f"🏆 {title}", _CHECKIN_RULE]
    medals = ("🥇", "🥈", "🥉")
    for i, u in enumerate(users, start=1):
        name = u.display_name or u.user_id
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        if by == "points":
            lines.append(f"{prefix} {name}")
            lines.append(f"   {u.points} 分 · 连签 {u.streak} 天")
        else:
            lines.append(f"{prefix} {name}")
            lines.append(f"   连签 {u.streak} 天 · {u.points} 分")
    return "\n".join(lines)


def format_task_view(*, name: str, task: PendingTask | None) -> str:
    if task is None:
        return (
            f"📝 今日任务\n"
            f"{name} 现在没有任务\n"
            f"先发「签到」抽一张～"
        )
    return (
        f"📝 今日任务\n"
        f"{name}\n"
        f"{_CHECKIN_RULE}\n"
        f"{task.text}\n"
        f"完成后发「完成任务」· +{task.reward} 分"
    )


def format_task_done(
    *,
    name: str,
    done_text: str,
    reward: int,
    total_points: int,
) -> str:
    return (
        f"✅ 任务验收通过\n"
        f"{name}，「{done_text}」学姐收下了～\n"
        f"{_CHECKIN_RULE}\n"
        f"本次 +{reward} 分\n"
        f"当前积分 {total_points}"
    )


def format_help() -> str:
    return (
        "📖 学姐签到玩法\n"
        f"{_CHECKIN_RULE}\n"
        "每日打卡\n"
        "· 签到 / 打卡\n"
        "  每天一次，连签加分；断签清零重计\n"
        "\n"
        "查看进度\n"
        "· 我的积分 —— 天数、称号、任务\n"
        "· 积分排行 / 连续排行 —— 看看谁在卷\n"
        "\n"
        "每日任务\n"
        "· 今日任务 —— 看当前任务\n"
        "· 完成任务 —— 做完来交作业加分\n"
        "\n"
        "连签满 3 / 7 / 15 / 30 天解锁称号\n"
        "签到有概率掉表情包哦～\n"
        "\n"
        "积分商店\n"
        "· 商店 —— 用积分给学姐买衣服换装\n"
        "· 购买 + 衣服名 —— 买下店里在售的款式"
    )


_store: CheckinStore | None = None


def get_checkin_store(data_dir: str | Path | None = None) -> CheckinStore:
    global _store
    if data_dir is not None:
        return CheckinStore(data_dir)
    if _store is None:
        from .config import get_config

        _store = CheckinStore(get_config().checkin_dir)
    return _store


def reset_checkin_store() -> None:
    global _store
    _store = None
