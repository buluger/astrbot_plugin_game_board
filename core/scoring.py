"""五档定级、分数解析、名称与标签归一。

纯函数，无外部依赖，便于单独测试。
"""

from __future__ import annotations

import re
import unicodedata

# 展示顺序：从高到低（与参考图一致）
TIERS: list[dict] = [
    {"key": "hang", "label": "夯", "min": 8.0, "max": 10.01, "bg": "#B33A27", "fg": "#FFFFFF"},
    {"key": "ding", "label": "顶级", "min": 6.0, "max": 8.0, "bg": "#D68A2E", "fg": "#FFFFFF"},
    {"key": "ren", "label": "人上人", "min": 4.0, "max": 6.0, "bg": "#F0D050", "fg": "#2B2B2B"},
    {"key": "npc", "label": "NPC", "min": 2.0, "max": 4.0, "bg": "#F6E79A", "fg": "#2B2B2B"},
    {"key": "la", "label": "拉完了", "min": 0.0, "max": 2.0, "bg": "#FFFFFF", "fg": "#2B2B2B"},
]

SCORE_MIN = 1.0
SCORE_MAX = 10.0

# 标签别名：键统一小写，值统一为 Steam 中文 genre 名或英文规范名
TAG_ALIASES: dict[str, str] = {
    "肉鸽": "roguelike",
    "rogue": "roguelike",
    "roguelite": "roguelike",
    "rpg": "角色扮演",
    "jrpg": "角色扮演",
    "srpg": "策略",
    "act": "动作",
    "act": "动作",
    "fps": "动作",
    "开放世界": "冒险",
    "openworld": "冒险",
    "galgame": "冒险",
    "avg": "冒险",
    "avg": "冒险",
    "解谜": "休闲",
    "puzzle": "休闲",
    "沙盒": "模拟",
    "sandbox": "模拟",
    "经营": "模拟",
    "模拟经营": "模拟",
    "卡牌": "策略",
    "roguelike卡牌": "策略",
}


def tier_of(score: float | None) -> dict:
    """按绝对阈值 2/4/6/8 把均分映射到五档。

    <2 拉完了 / 2-4 NPC / 4-6 人上人 / 6-8 顶级 / >=8 夯（8 分整算夯）
    """
    if score is None:
        return TIERS[-1]
    for tier in TIERS:
        if tier["min"] <= score < tier["max"]:
            return tier
    return TIERS[0] if score >= 8 else TIERS[-1]


def parse_score(text: str) -> float | None:
    """解析用户输入的分数，支持 8 / 8.5 / ８.５，越界返回 None。"""
    if text is None:
        return None
    raw = unicodedata.normalize("NFKC", str(text)).strip()
    raw = raw.replace("，", ".").replace(",", ".")
    m = re.fullmatch(r"(\d{1,2})(?:\.(\d))?", raw)
    if not m:
        return None
    value = float(raw)
    if value < SCORE_MIN or value > SCORE_MAX:
        return None
    return round(value, 1)


def fmt_score(score: float | None) -> str:
    if score is None:
        return "—"
    text = f"{score:.1f}"
    return text[:-2] if text.endswith(".0") else text


_PUNCT = re.compile(r"[\s\-_·:：,，.。!！?？'\"“”‘’()（）\[\]【】<>《》/\\|+&]")


def normalize_name(name: str) -> str:
    """名称归一：全角转半角、去标点空格、转小写。用于去重与模糊匹配。"""
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name).lower()
    return _PUNCT.sub("", text)


def normalize_tag(tag: str) -> str:
    """标签归一：先查别名表，再走名称归一。"""
    if not tag:
        return ""
    key = normalize_name(tag)
    if key in TAG_ALIASES:
        return normalize_name(TAG_ALIASES[key])
    return key


def tag_display(tag: str) -> str:
    """标签展示名：别名表里有的给规范写法，否则原样。"""
    key = normalize_name(tag)
    if key in TAG_ALIASES:
        return TAG_ALIASES[key]
    return tag.strip()


def relative_time(ts: int, now: int | None = None) -> str:
    """把时间戳转成「3 天前」这类相对时间。"""
    import time

    now = int(time.time()) if now is None else now
    delta = max(0, now - int(ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    if delta < 86400 * 30:
        return f"{delta // 86400} 天前"
    return f"{delta // (86400 * 30)} 个月前"
