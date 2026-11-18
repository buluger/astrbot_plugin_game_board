"""图片渲染：五档榜单表格图 + Steam 风格详情卡。

只依赖 Pillow。中文字体优先用服务器上的文泉驿，其次用 macOS 的 PingFang（本地调试）。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .scoring import TIERS, fmt_score, relative_time, tier_of

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]

BG = "#F4F5F7"
CARD_BG = "#FFFFFF"
TEXT_DARK = "#2B2B2B"
TEXT_GRAY = "#7A7F87"

DETAIL_BG = "#1B2838"
DETAIL_PANEL = "#22364A"
DETAIL_ACCENT = "#66C0F4"
DETAIL_TEXT = "#C6D4DF"
DETAIL_PIN = "#2A475E"

_board_cache: dict[int, ImageFont.FreeTypeFont] = {}
_font_path: str | None = None


def font_path() -> str:
    global _font_path
    if _font_path:
        return _font_path
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            _font_path = path
            return path
    raise RuntimeError("找不到可用的中文字体，请安装 wqy-microhei 或 Noto Sans CJK")


def font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _board_cache:
        _board_cache[size] = ImageFont.truetype(font_path(), size)
    return _board_cache[size]


def text_width(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> int:
    return int(draw.textlength(text, font=f))


def wrap_text(
    draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """按像素宽度折行，中英文通吃（逐字符累加）。"""
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        probe = current + ch
        if text_width(draw, probe, f) <= max_width or not current:
            current = probe
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def fit_font_size(
    draw: ImageDraw.ImageDraw, text: str, max_width: int, max_size: int, min_size: int = 14
) -> int:
    size = max_size
    while size > min_size and text_width(draw, text, font(size)) > max_width:
        size -= 2
    return size


def cover_image(path: str, size: tuple[int, int]) -> Image.Image:
    """读封面并裁成指定比例；失败返回占位图。"""
    w, h = size
    if path and Path(path).exists():
        try:
            img = Image.open(path).convert("RGB")
            return crop_to_ratio(img, w, h)
        except Exception:
            pass
    img = Image.new("RGB", (w, h), "#D9DDE3")
    d = ImageDraw.Draw(img)
    d.line([(0, 0), (w, h)], fill="#C3C9D1", width=2)
    d.line([(0, h), (w, 0)], fill="#C3C9D1", width=2)
    return img


def crop_to_ratio(img: Image.Image, w: int, h: int) -> Image.Image:
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h = img.height
        new_w = int(new_h * dst_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, new_h))
    else:
        new_w = img.width
        new_h = int(new_w / dst_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, new_w, top + new_h))
    return img.resize((w, h), Image.LANCZOS)


def ellipsize(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_w: int) -> str:
    """按像素宽度截断，超出补省略号。"""
    if text_width(draw, text, fnt) <= max_w:
        return text
    out = text
    while out and text_width(draw, out + "…", fnt) > max_w:
        out = out[:-1]
    return out + "…" if out else "…"


def circle_avatar(path: str, size: int, label: str = "") -> Image.Image:
    """圆形头像；没有图就画一个纯色圆 + 昵称首字。"""
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    if path and Path(path).exists():
        try:
            img = Image.open(path).convert("RGB")
            img = crop_to_ratio(img, size, size)
        except Exception:
            img = Image.new("RGB", (size, size), "#3E5A73")
    else:
        img = Image.new("RGB", (size, size), "#3E5A73")
        if label:
            ld = ImageDraw.Draw(img)
            f = font(max(12, size // 2))
            ch = label.strip()[:1]
            w = text_width(ld, ch, f)
            ld.text(((size - w) // 2, (size - f.size) // 2 - 1), ch, font=f, fill="#DCE9F5")
    out = Image.new("RGB", (size, size), "#000000")
    out.paste(img, (0, 0), mask)
    return out


# --------------------------------------------------------------------------- 榜单


def render_board(
    rows: list[dict],
    out_path: Path,
    title: str,
    subtitle: str = "",
    max_per_tier: int = 40,
) -> Path:
    width = 1240
    left_col = 170
    pad = 18
    cols = 4
    card_w = 250
    cover_h = int(card_w * 215 / 460)
    card_h = cover_h + 66
    gap_x = (width - left_col - pad * 2 - cols * card_w) // (cols - 1)
    gap_y = 16

    grouped: dict[str, list[dict]] = {tier["key"]: [] for tier in TIERS}
    for row in rows:
        tier = tier_of(row.get("avg_score") or 0)
        grouped[tier["key"]].append(row)

    heights: list[int] = []
    for tier in TIERS:
        items = grouped[tier["key"]]
        if not items:
            heights.append(92)  # 空档位压缩高度，别让图被空白撑长
            continue
        rows_count = max(1, math.ceil(len(items) / cols))
        heights.append(pad * 2 + rows_count * (card_h + gap_y) - gap_y)

    header_h = 96
    total_h = header_h + sum(heights) + 40

    img = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(img)

    # 顶部标题
    draw.text((left_col + pad, 22), title, font=font(34), fill=TEXT_DARK)
    if subtitle:
        draw.text((left_col + pad, 66), subtitle, font=font(18), fill=TEXT_GRAY)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    draw.text(
        (width - pad - text_width(draw, stamp, font(18)), 28),
        stamp,
        font=font(18),
        fill=TEXT_GRAY,
    )
    total_games = len(rows)
    count_text = f"共 {total_games} 个游戏"
    draw.text(
        (width - pad - text_width(draw, count_text, font(18)), 58),
        count_text,
        font=font(18),
        fill=TEXT_GRAY,
    )

    y = header_h
    for tier, row_h in zip(TIERS, heights, strict=False):
        draw.rectangle([0, y, width, y + row_h], fill=tier["bg"])
        draw.rectangle([left_col, y, width, y + row_h], fill=BG)
        # 左侧档位名：按宽度自适应
        size = fit_font_size(draw, tier["label"], left_col - 28, 50, 20)
        draw.text(
            (left_col // 2, y + row_h // 2),
            tier["label"],
            font=font(size),
            fill=tier["fg"],
            anchor="mm",
        )

        items = grouped[tier["key"]][:max_per_tier]
        if not items:
            draw.text(
                (left_col + pad + 40, y + row_h // 2),
                "暂无",
                font=font(26),
                fill="#9AA0A8",
                anchor="lm",
            )
        for idx, row in enumerate(items):
            r, c = divmod(idx, cols)
            x = left_col + pad + c * (card_w + gap_x)
            cy = y + pad + r * (card_h + gap_y)
            draw.rounded_rectangle(
                [x, cy, x + card_w, cy + card_h], radius=10, fill=CARD_BG
            )
            cover = cover_image(row.get("cover_path") or "", (card_w, cover_h))
            mask = Image.new("L", (card_w, cover_h), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, card_w, cover_h], radius=10, fill=255
            )
            img.paste(cover, (x, cy), mask)
            name = row.get("name_cn") or row.get("name") or "未知游戏"
            name_font = font(20)
            if text_width(draw, name, name_font) > card_w - 16:
                name = name[: 12] + "…"
            draw.text((x + 8, cy + cover_h + 6), name, font=name_font, fill=TEXT_DARK)
            score_text = f"{fmt_score(row.get('avg_score'))} 分 · {row.get('votes', 0)} 人"
            draw.text((x + 8, cy + cover_h + 32), score_text, font=font(17), fill=TEXT_GRAY)
            if row.get("votes", 0) == 0:
                draw.text(
                    (x + card_w - 56, cy + cover_h + 8), "待评", font=font(16), fill="#B33A27"
                )
        y += row_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


# --------------------------------------------------------------------------- 详情


def render_detail(
    game: dict,
    comments: list[dict],
    out_path: Path,
    *,
    max_comments: int = 6,
    pin_recommender: bool = True,
    avatar_dir: Path | None = None,
    ratings: list[dict] | None = None,
    max_ratings: int = 12,
) -> Path:
    width = 900
    margin = 36
    content_w = width - margin * 2

    # 先算高度（用一次性画布做度量）
    probe = Image.new("RGB", (width, 10), DETAIL_BG)
    pd = ImageDraw.Draw(probe)

    desc_font = font(20)
    desc_lines = wrap_text(pd, game.get("desc") or "暂无简介。", desc_font, content_w - 40)
    desc_lines = desc_lines[:3]

    ordered = list(comments)
    if pin_recommender:
        ordered.sort(key=lambda c: (0 if c.get("pinned") else 1, c.get("created_at", 0)))
    shown = ordered[:max_comments]
    remain = max(0, len(ordered) - len(shown))

    comment_blocks: list[tuple[dict, int, list[str]]] = []
    for c in shown:
        text_lines = wrap_text(pd, c.get("text", ""), font(20), content_w - 120)
        block_h = 46 + 24 * len(text_lines) + 16
        comment_blocks.append((c, block_h, text_lines))

    all_ratings = sorted(
        ratings or [], key=lambda r: (-float(r.get("score") or 0), r.get("updated_at", 0))
    )
    shown_ratings = all_ratings[:max_ratings]
    rating_remain = max(0, len(all_ratings) - len(shown_ratings))
    ratings_title_h = 64 if shown_ratings else 0
    ratings_h = (
        40 * len(shown_ratings) + (26 if rating_remain else 0) + 18
        if shown_ratings
        else 0
    )

    header_h = 300
    title_h = 126
    desc_h = 30 + 26 * len(desc_lines) + 24
    comment_title_h = 56
    comments_h = sum(b[1] for b in comment_blocks) + (34 if remain else 0) + 20
    total_h = (
        header_h + title_h + desc_h + comment_title_h + comments_h
        + ratings_title_h + ratings_h + 24
    )

    img = Image.new("RGB", (width, total_h), DETAIL_BG)
    draw = ImageDraw.Draw(img)

    # 顶部大图 + 渐变遮罩
    header = cover_image(game.get("header_path") or game.get("cover_path") or "", (width, header_h))
    img.paste(header, (0, 0))
    overlay = Image.new("RGBA", (width, header_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(header_h):
        alpha = int(220 * max(0.0, (i - header_h * 0.35) / (header_h * 0.65)))
        od.line([(0, i), (width, i)], fill=(27, 40, 56, min(alpha, 235)))
    img.paste(Image.alpha_composite(header.convert("RGBA"), overlay).convert("RGB"), (0, 0))

    # 标题
    title = game.get("name_cn") or game.get("name") or "未知游戏"
    size = fit_font_size(draw, title, content_w - 200, 40, 22)
    draw.text((margin, header_h - 66), title, font=font(size), fill="#FFFFFF")
    alt = game.get("name") or ""
    if alt and alt != title:
        draw.text((margin, header_h - 26), alt, font=font(17), fill="#8FACC2")

    y = header_h

    # 评分块
    score = game.get("avg_score") or 0
    votes = game.get("votes") or 0
    tier = tier_of(score)
    score_text = fmt_score(score)
    draw.text((margin, y + 8), score_text, font=font(56), fill=DETAIL_ACCENT)
    sx = margin + text_width(draw, score_text, font(56)) + 8
    draw.text((sx, y + 30), "/10", font=font(20), fill="#8FACC2")
    votes_text = f"{votes} 人打分"
    draw.text((sx + 4, y + 58), votes_text, font=font(17), fill="#8FACC2")
    badge_w = text_width(draw, tier["label"], font(22)) + 32
    draw.rounded_rectangle(
        [width - margin - badge_w, y + 18, width - margin, y + 58],
        radius=8,
        fill=tier["bg"],
    )
    label_size = fit_font_size(draw, tier["label"], badge_w - 20, 24, 14)
    draw.text(
        (width - margin - badge_w + 16, y + 28),
        tier["label"],
        font=font(label_size),
        fill=tier["fg"],
    )

    # 标签胶囊
    tags = list(dict.fromkeys((game.get("tags") or []) + (game.get("custom_tags") or [])))[:6]
    tx = margin
    ty = y + 78
    for tag in tags:
        tw = text_width(draw, tag, font(17)) + 24
        if tx + tw > width - margin:
            break
        draw.rounded_rectangle([tx, ty, tx + tw, ty + 30], radius=15, fill=DETAIL_PANEL)
        draw.text((tx + 12, ty + 5), tag, font=font(17), fill=DETAIL_ACCENT)
        tx += tw + 10
    y += title_h

    # 简介
    draw.text((margin, y), "简介", font=font(19), fill="#8FACC2")
    y += 28
    for line in desc_lines:
        draw.text((margin, y), line, font=desc_font, fill=DETAIL_TEXT)
        y += 26
    y += 24

    # 评论标题
    draw.line([(margin, y), (width - margin, y)], fill="#2F4559", width=1)
    y += 16
    draw.text((margin, y), f"群友评论（{len(comments)}）", font=font(22), fill="#FFFFFF")
    y += 40

    avatar_size = 40
    for comment, block_h, text_lines in comment_blocks:
        pinned = bool(comment.get("pinned"))
        if pinned:
            draw.rounded_rectangle(
                [margin - 10, y - 8, width - margin + 10, y + block_h - 6],
                radius=10,
                fill=DETAIL_PIN,
            )
        ax = margin
        ay = y
        avatar_file = None
        if avatar_dir and comment.get("avatar"):
            avatar_file = str(Path(avatar_dir) / comment["avatar"])
        name = comment.get("nickname") or comment.get("user_id") or "群友"
        av = circle_avatar(avatar_file or "", avatar_size, name)
        img.paste(av, (ax, ay))
        text_x = ax + avatar_size + 12
        if pinned:
            pill_w = text_width(draw, "置顶", font(14)) + 16
            draw.rounded_rectangle(
                [text_x, ay + 2, text_x + pill_w, ay + 24], radius=6, fill="#C7D5E0"
            )
            draw.text((text_x + 8, ay + 6), "置顶", font=font(14), fill="#1B2838")
            text_x += pill_w + 8
        draw.text(
            (text_x, ay),
            f"{name} · 推荐人" if pinned else name,
            font=font(19),
            fill="#FFFFFF" if pinned else DETAIL_ACCENT,
        )
        meta = relative_time(comment.get("created_at", 0))
        if pinned:
            meta = "推荐人 · " + meta
        meta_x = ax + avatar_size + 12
        draw.text((meta_x, ay + 26), meta, font=font(15), fill="#8FACC2")
        ty = ay + 48
        for line in text_lines:
            draw.text((ax + avatar_size + 12, ty), line, font=font(20), fill="#FFFFFF")
            ty += 24
        y += block_h

    if remain:
        draw.text(
            (margin, y),
            f"还有 {remain} 条评论，发 /榜单详情 名字 可随时再看",
            font=font(16),
            fill="#8FACC2",
        )
        y += 34

    # 群友打分（分数条形图，从高到低）
    if shown_ratings:
        y += 14
        draw.line([(margin, y), (width - margin, y)], fill="#2F4559", width=1)
        y += 16
        draw.text(
            (margin, y), f"群友打分（{len(all_ratings)}）", font=font(22), fill="#FFFFFF"
        )
        y += 40

        avatar_size = 32
        name_w = 168
        bar_x = margin + avatar_size + 12 + name_w
        score_col_w = 56
        bar_w = width - margin - score_col_w - 16 - bar_x
        for r in shown_ratings:
            score_value = float(r.get("score") or 0)
            tier = tier_of(score_value)
            name = r.get("nickname") or str(r.get("user_id") or "群友")
            face = None
            if avatar_dir and r.get("avatar"):
                face = str(Path(avatar_dir) / r["avatar"])
            img.paste(circle_avatar(face or "", avatar_size, name), (margin, y + 4))

            label = ellipsize(draw, name, font(18), name_w)
            draw.text(
                (margin + avatar_size + 12, y + 8), label, font=font(18), fill=DETAIL_TEXT
            )

            track_y = y + 15
            draw.rounded_rectangle(
                [bar_x, track_y, bar_x + bar_w, track_y + 12], radius=6, fill=DETAIL_PANEL
            )
            fill_w = max(6, int(bar_w * min(score_value, 10.0) / 10.0))
            fill_color = "#C7D5E0" if tier["bg"] == "#FFFFFF" else tier["bg"]
            draw.rounded_rectangle(
                [bar_x, track_y, bar_x + fill_w, track_y + 12], radius=6, fill=fill_color
            )

            score_label = fmt_score(score_value)
            draw.text(
                (width - margin - text_width(draw, score_label, font(19)), y + 7),
                score_label,
                font=font(19),
                fill="#FFFFFF",
            )
            y += 40

        if rating_remain:
            draw.text(
                (margin, y),
                f"还有 {rating_remain} 位群友打过分数",
                font=font(16),
                fill="#8FACC2",
            )
            y += 26
        y += 18

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path
