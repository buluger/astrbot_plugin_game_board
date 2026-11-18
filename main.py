"""群游戏推荐榜 —— AstrBot 插件主入口。

指令：/推荐 /评分 /评论 /撤评 /撤榜 /榜单 /小众游戏 /游戏
"""

from __future__ import annotations

import asyncio
import difflib
import re
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image as CompImage, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .core.db import GameStore
from .core.render import render_board, render_detail
from .core.scoring import (
    fmt_score,
    normalize_name,
    normalize_tag,
    parse_score,
    tier_of,
)
from .core.sources import BangumiSource, Candidate, GameInfo, SteamSource, download, safe_filename

PLUGIN_NAME = "astrbot_plugin_game_board"
DATA_NAME = "game_board"
AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100"

USAGE = (
    "用法：\n"
    "/推荐 游戏名 —— 推荐游戏并打分\n"
    "/评分 游戏名 分数 [评论] —— 打分（1-10，可带 .5）\n"
    "/评论 游戏名 —— 给游戏补一条评论\n"
    "/游戏 游戏名 —— 看详情\n"
    "/榜单 [标签...] —— 看总榜 / 按标签看\n"
    "/小众游戏 —— 打分人数不足的游戏\n"
    "/撤评 游戏名 —— 撤销自己的评分\n"
    "/撤榜 游戏名 —— 移除推荐（推荐人/管理员）"
)


@register(
    PLUGIN_NAME,
    "buluger",
    "群友推荐游戏、打分评论，自动抓取封面标签，输出五档榜单与 Steam 风格详情卡",
    "0.1.0",
    repo="https://github.com/buluger/astrbot_plugin_game_board",
)
class GameBoardPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.cfg = config or {}
        self.data_dir = StarTools.get_data_dir(DATA_NAME)
        self.cover_dir = self.data_dir / "covers"
        self.avatar_dir = self.data_dir / "avatars"
        self.image_dir = self.data_dir / "images"
        for d in (self.cover_dir, self.avatar_dir, self.image_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.db = GameStore(self.data_dir)
        self.steam = SteamSource(
            language=str(self._cfg("steam_language", "schinese")),
            country=str(self._cfg("steam_country", "cn")),
        )
        self.bangumi = BangumiSource()
        self._rate: dict[str, list[float]] = {}
        self._pending: dict[str, dict] = {}

    # ------------------------------------------------------------------ 基础工具

    def _cfg(self, key: str, default):
        try:
            value = self.cfg.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _sender(self, event: AstrMessageEvent) -> tuple[str, str]:
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = ""
        try:
            nick = event.get_sender_name() or uid
        except Exception:
            nick = uid
        return uid, nick

    def _group(self, event: AstrMessageEvent) -> str:
        """群号；私聊退化为 private:<uid>，保证数据仍然隔离。"""
        try:
            gid = event.get_group_id()
        except Exception:
            gid = None
        if not gid:
            gid = getattr(getattr(event, "message_obj", None), "group_id", "") or ""
        if not gid:
            uid, _ = self._sender(event)
            return f"private:{uid}"
        return str(gid)

    def _rest(self, event: AstrMessageEvent, names: list[str]) -> str:
        """取指令后面的参数。

        兼容：带 / ! 前缀、被 @ 提前、以及消息里只有指令没有参数的情况。
        """
        text = (getattr(event, "message_str", "") or "").strip()
        wanted = {n for n in names}
        tokens = text.split()
        for i, token in enumerate(tokens[:3]):
            bare = token.lstrip("/／!！").strip()
            if bare in wanted:
                return " ".join(tokens[i + 1:]).strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _rate_ok(self, uid: str) -> bool:
        now = time.time()
        window = max(1, int(self._cfg("rate_limit_window", 10)))
        limit = max(1, int(self._cfg("rate_limit_count", 3)))
        marks = [t for t in self._rate.get(uid, []) if now - t < window]
        marks.append(now)
        self._rate[uid] = marks
        return len(marks) <= limit

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            role = getattr(event, "role", None) or getattr(
                getattr(event, "message_obj", None), "sender", None
            ).role
            return str(role).lower() in {"admin", "owner"}
        except Exception:
            return False

    async def _avatar_file(self, uid: str) -> str:
        """下载 QQ 头像到本地，返回文件名；失败返回空串。"""
        if not uid:
            return ""
        fname = f"{safe_filename(uid)}.jpg"
        path = self.avatar_dir / fname
        if path.exists() and path.stat().st_size > 0:
            return fname
        ok = await download(AVATAR_URL.format(uid=uid), path)
        return fname if ok else ""

    @staticmethod
    async def _reply(event: AstrMessageEvent, text: str) -> None:
        """在 session_waiter 里没法 yield，只能直接发。"""
        await event.send(MessageChain([Plain(text)]))

    def _image_path(self, name: str) -> Path:
        return self.image_dir / f"{safe_filename(name)}_{int(time.time() * 1000)}.png"

    # ------------------------------------------------------------------ 数据源

    async def _search(self, term: str) -> list[Candidate]:
        """搜索游戏。

        Steam 只认官方译名，很多中文俗称（如「空洞骑士」「星露谷物语」）搜不到，
        所以先走 Steam，失败后用 Bangumi 拿到原名，再拿原名回 Steam 查一次，
        这样既能显示中文名，又能拿到 Steam 的封面与标签。
        """
        steam_enabled = bool(self._cfg("enable_steam", True))
        bgm_enabled = bool(self._cfg("enable_bangumi", True))

        if steam_enabled:
            try:
                direct = await self.steam.search(term)
                if direct:
                    return direct
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[game_board] Steam 搜索失败：{exc}")

        if not bgm_enabled:
            return []

        try:
            bgm = await self.bangumi.search(term)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[game_board] Bangumi 搜索失败：{exc}")
            return []

        if not bgm:
            return []

        if not steam_enabled:
            return bgm

        merged: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        for alt in bgm[:3]:
            for query in {alt.name, alt.name_cn} - {""}:
                try:
                    hits = await self.steam.search(query, limit=3)
                except Exception:  # noqa: BLE001
                    continue
                for hit in hits:
                    key = (hit.source, hit.source_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    if alt.name_cn:
                        hit.name_cn = alt.name_cn
                    merged.append(hit)
        return merged or bgm

    async def _fetch_info(self, cand: Candidate) -> GameInfo | None:
        try:
            if cand.source == "steam":
                return await self.steam.detail(cand.source_id)
            return await self.bangumi.detail(cand.source_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[game_board] 拉取详情失败：{exc}")
            return None

    async def _ensure_assets(self, game: dict) -> dict:
        """确保封面 / 大图已下载到本地，并把路径写回数据库。"""
        key = f"{game['source']}_{safe_filename(str(game['source_id']))}"
        cover = self.cover_dir / f"{key}.jpg"
        header = self.cover_dir / f"{key}_h.jpg"
        cover_url = game.get("cover_url") or ""
        header_url = game.get("header_url") or ""
        cover_ok = False
        header_ok = False
        if cover_url and not cover.exists():
            cover_ok = await download(cover_url, cover)
        if header_url and not header.exists():
            await download(header_url, header)
        if not header.exists() and game.get("header_fallback"):
            await download(game["header_fallback"], header)
        if not header.exists() and cover.exists():
            shutil.copyfile(cover, header)
        cover_path = str(cover) if cover.exists() else game.get("cover_path") or ""
        header_path = str(header) if header.exists() else game.get("header_path") or ""
        if cover_path != (game.get("cover_path") or "") or header_path != (
            game.get("header_path") or ""
        ):
            await self.db.update_game_assets(
                game["id"], cover_path=cover_path, header_path=header_path
            )
        game["cover_path"] = cover_path
        game["header_path"] = header_path
        return game

    async def _register_game(
        self,
        info: GameInfo,
        group: str,
        uid: str,
        *,
        extra_tags: list[str] | None = None,
        display_name: str = "",
    ) -> dict:
        existing = await self.db.find_game(info.source, info.source_id)
        if existing:
            return existing
        tags = list(dict.fromkeys((info.tags or []) + (extra_tags or [])))
        game_id = await self.db.add_game(
            source=info.source,
            source_id=info.source_id,
            name=info.name,
            name_cn=display_name or info.name_cn or info.name,
            name_norm=normalize_name(info.name_cn or info.name),
            tags=tags,
            desc=info.desc,
            cover_url=info.cover_url,
            header_url=info.header_url,
            manual_group="",
            created_by=uid,
        )
        game = await self.db.get_game(game_id)
        assert game is not None
        return await self._ensure_assets(game)

    # ------------------------------------------------------------------ 交互流程

    async def _ask_score(
        self, event: AstrMessageEvent, title: str
    ) -> tuple[float | None, str]:
        """等待用户输入分数（可附评论），90 秒超时。"""
        umo = event.unified_msg_origin
        state: dict = {"score": None, "comment": ""}
        self._pending[umo] = state

        @session_waiter(timeout=90)
        async def wait_score(controller: SessionController, sub_event: AstrMessageEvent):
            text = (sub_event.message_str or "").strip()
            if text in {"取消", "cancel", "退出"}:
                controller.stop()
                return
            tokens = text.split(maxsplit=1)
            score = parse_score(tokens[0]) if tokens else None
            if score is None:
                await self._reply(
                    sub_event, "要 1-10 之间的分数，例如 8 或 9.5（可附一句评论）"
                )
                controller.keep(90, reset_timeout=True)
                return
            state["score"] = score
            state["comment"] = tokens[1].strip() if len(tokens) > 1 else ""
            controller.stop()

        try:
            await wait_score(event)
        except TimeoutError:
            return None, ""
        finally:
            self._pending.pop(umo, None)
        return state["score"], state["comment"]

    async def _ask_comment(self, event: AstrMessageEvent) -> str:
        @session_waiter(timeout=90)
        async def wait_comment(controller: SessionController, sub_event: AstrMessageEvent):
            text = (sub_event.message_str or "").strip()
            if text in {"取消", "cancel", "退出"}:
                controller.stop()
                return
            if not text:
                controller.keep(90, reset_timeout=True)
                return
            self._pending[event.unified_msg_origin] = {"comment": text}
            controller.stop()

        try:
            await wait_comment(event)
        except TimeoutError:
            return ""
        return (self._pending.pop(event.unified_msg_origin, {}) or {}).get("comment", "")

    async def _ask_text(self, event: AstrMessageEvent, prompt: str, timeout: int = 90) -> str:
        """发一条提示，等用户回文本。"""
        umo = event.unified_msg_origin
        box: dict = {"text": ""}

        @session_waiter(timeout=timeout)
        async def wait_text(controller: SessionController, sub_event: AstrMessageEvent):
            text = (sub_event.message_str or "").strip()
            if text in {"取消", "cancel", "退出"}:
                controller.stop()
                return
            if not text:
                controller.keep(timeout, reset_timeout=True)
                return
            box["text"] = text
            controller.stop()

        await self._reply(event, prompt)
        try:
            await wait_text(event)
        except TimeoutError:
            return ""
        finally:
            self._pending.pop(umo, None)
        return box["text"]

    async def _ask_image(self, event: AstrMessageEvent, timeout: int = 90) -> Path | None:
        """等用户发一张封面图；发「跳过」返回 None。"""
        state: dict = {"path": None}

        @session_waiter(timeout=timeout)
        async def wait_image(controller: SessionController, sub_event: AstrMessageEvent):
            text = (sub_event.message_str or "").strip()
            if text in {"跳过", "skip", "取消"}:
                controller.stop()
                return
            for comp in getattr(sub_event.message_obj, "message", []) or []:
                if not isinstance(comp, CompImage):
                    continue
                url = getattr(comp, "url", "") or ""
                local = getattr(comp, "file", "") or ""
                dest = self.cover_dir / f"manual_{int(time.time() * 1000)}.jpg"
                if local and Path(local).exists():
                    try:
                        shutil.copyfile(local, dest)
                        state["path"] = dest
                    except Exception:
                        state["path"] = None
                elif url:
                    if await download(url, dest):
                        state["path"] = dest
                controller.stop()
                return
            await self._reply(sub_event, "没看到图片，重新发一张，或发「跳过」。")
            controller.keep(timeout, reset_timeout=True)

        await self._reply(event, "发一张游戏封面图（60 秒内），或者发「跳过」用占位图：")
        try:
            await wait_image(event)
        except TimeoutError:
            return None
        return state["path"]

    async def _manual_create(
        self, event: AstrMessageEvent, term: str, group: str, uid: str
    ) -> dict | None:
        """手动建条目：封面 + 标签，来源标记 manual，仅本群可见。"""
        cover = await self._ask_image(event)
        tags_text = await self._ask_text(
            event, "给它几个标签，空格分隔（例如：动作 独立），发「跳过」就不打标签："
        )
        tags = [t for t in re.split(r"[\s,，]+", tags_text) if t and t not in {"跳过", "skip"}]
        source_id = f"manual_{int(time.time())}_{uid}"
        game_id = await self.db.add_game(
            source="manual",
            source_id=source_id,
            name=term,
            name_cn=term,
            name_norm=normalize_name(term),
            tags=tags,
            desc="",
            cover_path=str(cover) if cover else "",
            header_path=str(cover) if cover else "",
            manual_group=group,
            created_by=uid,
        )
        return await self.db.get_game(game_id)

    async def _choose_candidate(
        self, event: AstrMessageEvent, candidates: list[Candidate]
    ) -> Candidate | None:
        lines = [
            f"{i}. {c.name_cn or c.name}"
            + (f"（{c.name}）" if c.name_cn and c.name != c.name_cn else "")
            + (f" · Steam {c.source_id}" if c.source == "steam" else " · Bangumi")
            for i, c in enumerate(candidates, 1)
        ]
        yield_msg = "\n".join(lines)
        state: dict = {"pick": 0}
        self._pending[event.unified_msg_origin] = state

        @session_waiter(timeout=90)
        async def wait_pick(controller: SessionController, sub_event: AstrMessageEvent):
            text = (sub_event.message_str or "").strip()
            if text in {"0", "取消", "cancel"}:
                controller.stop()
                return
            if not text.isdigit() or not (1 <= int(text) <= len(candidates)):
                await self._reply(sub_event, f"回 1-{len(candidates)} 的数字选，0 取消")
                controller.keep(90, reset_timeout=True)
                return
            state["pick"] = int(text)
            controller.stop()

        await self._reply(event, f"找到这些，回数字选一个：\n{yield_msg}")
        try:
            await wait_pick(event)
        except TimeoutError:
            return None
        self._pending.pop(event.unified_msg_origin, None)
        pick = state.get("pick", 0)
        return candidates[pick - 1] if pick else None

    # ------------------------------------------------------------------ 指令

    @filter.command("榜帮助", alias={"游戏帮助", "gamehelp", "游戏指令"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看插件全部指令"""
        group = self._group(event)
        rows = await self.db.board(group)
        votes = sum(int(r.get("votes") or 0) for r in rows)
        total = len(rows)
        niche = sum(1 for r in rows if int(r.get("votes") or 0) < int(self._cfg("min_votes_niche", 5)))

        board_title = await self.db.get_board_title(group) or "游戏推荐榜"
        lines = [
            f"「{board_title}」· 指令一览",
            "",
            "/上榜 游戏名 —— 搜索后打 1-10 分上榜，可带一句评论",
            "/打分 游戏名 分数 [评论] —— 投票或改票，如 /打分 杀戮尖塔 9.5 神作",
            "/点评 游戏名 —— 只补评论，不打分",
            "/详情 游戏名 —— 详情卡，推荐人评论置顶",
            "/榜单 [标签...] —— 总榜；带标签则筛选，空格是并集，动作+独立 是交集",
            "/小众榜 —— 打分人数不足 5 人的游戏单独成榜",
            "/贴标签 游戏名 标签... —— 给作品补标签，榜单可按它筛",
            "/撤分 游戏名 —— 撤销自己那一票",
            "/下榜 游戏名 —— 从本群移除（推荐人或管理员）",
            "/榜名 名称 —— 改本群榜单标题；/榜名 重置 恢复默认",
            "",
            "分档：<2 拉完了 · 2-4 NPC · 4-6 人上人 · 6-8 顶级 · ≥8 夯",
            "英文别名：recommend / rate / comment / game / board / niche / tag / unrate / remove",
            "",
            f"本群现有 {total} 个游戏、{votes} 票"
            + (f"，其中 {niche} 个是小众榜候选" if niche else ""),
            "上手顺序：/上榜 艾尔登法环 → 发 9.5 神作 → /榜单",
        ]
        if not total:
            lines.append("（榜单还是空的，先来第一个上榜游戏吧）")
        yield event.plain_result("\n".join(lines))

    @filter.command("上榜", alias={"推荐", "recommend"})
    async def cmd_recommend(self, event: AstrMessageEvent):
        """推荐一个游戏并打分上榜"""
        uid, nick = self._sender(event)
        if not self._rate_ok(uid):
            yield event.plain_result("操作太频繁了，缓一缓再来。")
            return
        term = self._rest(event, ["上榜", "推荐", "recommend"])
        if not term:
            yield event.plain_result("要推荐什么游戏？用法：/推荐 艾尔登法环")
            return

        group = self._group(event)
        board = await self.db.board(group)
        same = self._match(board, term)
        if same:
            yield event.plain_result(
                f"「{same.get('name_cn') or same['name']}」已经在榜单里了："
                f"{fmt_score(same.get('avg_score'))} 分 / {same.get('votes', 0)} 人。\n"
                f"直接 /打分 {same.get('name_cn') or same['name']} 分数 给它投票吧。"
            )
            return

        try:
            candidates = await self._search(term)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[game_board] 搜索失败：{exc}")
            yield event.plain_result("搜索出错了，稍后再试。")
            return

        if not candidates:
            if not self._cfg("allow_manual", True):
                yield event.plain_result(f"没搜到「{term}」，Steam / Bangumi 都没有收录。")
                return
            yield event.plain_result(
                f"Steam 和 Bangumi 都没搜到「{term}」。\n如果是手游或小众作品，我给它手动建个条目。"
            )
            manual_game = await self._manual_create(event, term, group, uid)
            if not manual_game:
                yield event.plain_result("没建成功，稍后再试。")
                return
            await self.db.add_submission(group, manual_game["id"], uid)
            yield event.plain_result(
                f"《{term}》已手动加入（仅本群可见）。给它打个分吧（1-10，可附评论）："
            )
            score, comment = await self._ask_score(event, term)
            if score is None:
                yield event.plain_result("没等到分数，这次先不算分，随时 /评分 补上。")
                return
            avatar = await self._avatar_file(uid)
            await self.db.set_rating(group, manual_game["id"], uid, score, nick, avatar)
            if comment:
                await self.db.add_comment(
                    group, manual_game["id"], uid, comment, nick, avatar, pinned=True
                )
            tier = tier_of(score)
            yield event.plain_result(
                f"已上榜：《{term}》{fmt_score(score)} 分（{tier['label']}）"
            )
            return

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chosen = await self._choose_candidate(event, candidates)
            if not chosen:
                yield event.plain_result("已取消。")
                return

        info = await self._fetch_info(chosen)
        if not info:
            yield event.plain_result("拉到详情失败了，换一个名字试试。")
            return

        game = await self._register_game(
            info, group, uid, display_name=chosen.name_cn or info.name_cn
        )
        await self.db.add_submission(group, game["id"], uid)

        yield event.plain_result(
            f"《{info.name_cn or info.name}》已加入候选，"
            f"标签：{'、'.join(info.tags[:4]) or '无'}。\n"
            "给它打个分吧（1-10，可带评论，例如「9.5 这几年最好的魂游」）："
        )
        score, comment = await self._ask_score(event, info.name_cn or info.name)
        if score is None:
            yield event.plain_result("没等到分数，这次推荐先不算分，随时 /评分 补上。")
            return

        avatar = await self._avatar_file(uid)
        await self.db.set_rating(group, game["id"], uid, score, nick, avatar)
        if comment:
            await self.db.add_comment(
                group, game["id"], uid, comment, nick, avatar, pinned=True
            )
        tier = tier_of(score)
        yield event.plain_result(
            f"已上榜：《{info.name_cn or info.name}》{fmt_score(score)} 分"
            f"（{'夯' if tier['key'] == 'hang' else tier['label']}）\n"
            "其他群友可以 /打分 名字 分数 来投票，/榜单 看全群排行。"
        )

    @filter.command("打分", alias={"评分", "rate"})
    async def cmd_rate(self, event: AstrMessageEvent):
        """给已有游戏打分"""
        uid, nick = self._sender(event)
        if not self._rate_ok(uid):
            yield event.plain_result("操作太频繁了，缓一缓再来。")
            return
        rest = self._rest(event, ["打分", "评分", "rate"])
        tokens = rest.split()
        score_index = next(
            (i for i, tok in enumerate(tokens) if parse_score(tok) is not None), -1
        )
        if score_index <= 0:
            yield event.plain_result("用法：/打分 游戏名 分数 [评论]，例如 /打分 杀戮尖塔 9")
            return
        score = parse_score(tokens[score_index])
        name = " ".join(tokens[:score_index]).strip()
        comment = " ".join(tokens[score_index + 1:]).strip()
        assert score is not None

        group = self._group(event)
        board = await self.db.board(group)
        game = self._match(board, name)
        if not game:
            yield event.plain_result(f"榜单里没有「{name}」，先用 /上榜 加进来。")
            return
        old = await self.db.my_rating(group, game["id"], uid)
        avatar = await self._avatar_file(uid)
        await self.db.set_rating(group, game["id"], uid, score, nick, avatar)
        stats = await self.db.stats(group, game["id"])
        tier = tier_of(stats.get("avg_score") if stats else score)
        text = (
            f"已记录：{game.get('name_cn') or game['name']} {fmt_score(score)} 分"
            f"（你此前的分：{fmt_score(old) if old is not None else '无'}）\n"
            f"当前均分 {fmt_score(stats.get('avg_score'))} / {stats.get('votes', 0)} 人"
            f" → {tier['label']}"
        )
        if comment:
            pinned = (game.get("recommender") or "") == uid
            await self.db.add_comment(group, game["id"], uid, comment, nick, avatar, pinned=pinned)
            text += "\n评论也记下了。"
        yield event.plain_result(text)

    @filter.command("点评", alias={"评论", "comment"})
    async def cmd_comment(self, event: AstrMessageEvent):
        """给游戏补一条评论"""
        uid, nick = self._sender(event)
        if not self._rate_ok(uid):
            yield event.plain_result("操作太频繁了，缓一缓再来。")
            return
        name = self._rest(event, ["点评", "评论", "comment"])
        if not name:
            yield event.plain_result("用法：/点评 游戏名（然后发内容）")
            return
        group = self._group(event)
        board = await self.db.board(group)
        game = self._match(board, name)
        if not game:
            yield event.plain_result(f"榜单里没有「{name}」。")
            return

        max_len = int(self._cfg("comment_max_len", 200))
        max_count = int(self._cfg("comment_max_per_game", 20))
        count = await self.db.count_comment_of_user(group, game["id"], uid)
        if count >= max_count:
            yield event.plain_result(f"你在《{game.get('name_cn') or game['name']}》下已经评论 {count} 条了，够啦。")
            return

        await self._reply(
            event,
            f"想对《{game.get('name_cn') or game['name']}》说点什么？直接发内容（{max_len} 字内）：",
        )
        text = await self._ask_comment(event)
        if not text:
            yield event.plain_result("没等到内容，这次不记了。")
            return
        text = text[:max_len]
        avatar = await self._avatar_file(uid)
        pinned = (game.get("recommender") or "") == uid
        await self.db.add_comment(group, game["id"], uid, text, nick, avatar, pinned=pinned)
        yield event.plain_result("评论已记录，/游戏 名字 可以看全部评论。")

    @filter.command("撤分", alias={"撤评", "unrate"})
    async def cmd_unrate(self, event: AstrMessageEvent):
        """撤销自己的评分"""
        uid, _ = self._sender(event)
        name = self._rest(event, ["撤分", "撤评", "unrate"])
        if not name:
            yield event.plain_result("用法：/撤分 游戏名")
            return
        group = self._group(event)
        board = await self.db.board(group)
        game = self._match(board, name)
        if not game:
            yield event.plain_result(f"榜单里没有「{name}」。")
            return
        removed = await self.db.del_rating(group, game["id"], uid)
        if not removed:
            yield event.plain_result("你还没给这个游戏打过分。")
            return
        stats = await self.db.stats(group, game["id"])
        yield event.plain_result(
            f"已撤销你在《{game.get('name_cn') or game['name']}》的评分，"
            f"现在 {fmt_score(stats.get('avg_score'))} 分 / {stats.get('votes', 0)} 人。"
        )

    @filter.command("下榜", alias={"撤榜", "remove"})
    async def cmd_remove(self, event: AstrMessageEvent):
        """移除推荐（推荐人本人或管理员）"""
        uid, _ = self._sender(event)
        name = self._rest(event, ["下榜", "撤榜", "remove"])
        if not name:
            yield event.plain_result("用法：/下榜 游戏名")
            return
        group = self._group(event)
        board = await self.db.board(group)
        game = self._match(board, name)
        if not game:
            yield event.plain_result(f"榜单里没有「{name}」。")
            return
        allowed = (game.get("recommender") or "") == uid or self._is_admin(event)
        if not allowed:
            yield event.plain_result("只有推荐人本人或群管理员能撤榜。")
            return
        await self.db.delete_game_from_group(group, game["id"])
        yield event.plain_result(f"已把《{game.get('name_cn') or game['name']}》从本群榜单移除。")

    @filter.command("贴标签", alias={"加标签", "tag"})
    async def cmd_tag(self, event: AstrMessageEvent):
        """给作品补充标签（本群生效，可被 /榜单 筛选）"""
        uid, _ = self._sender(event)
        if not self._rate_ok(uid):
            yield event.plain_result("操作太频繁了，缓一缓再来。")
            return
        rest = self._rest(event, ["贴标签", "加标签", "tag"])
        if not rest:
            yield event.plain_result("用法：/贴标签 游戏名 标签1 标签2，例如 /贴标签 空洞骑士 魂系 手绘")
            return
        group = self._group(event)
        rows = await self.db.board(group)
        if not rows:
            yield event.plain_result("本群榜单还是空的，先 /上榜 加个游戏吧。")
            return

        game, raw_tags = self._split_game_and_tags(rows, rest)
        if not game:
            yield event.plain_result(
                f"没认出「{rest}」是哪个游戏。用法：/贴标签 游戏名 标签1 标签2\n"
                "名字不确定的话先发 /榜单 看一眼。"
            )
            return

        tags = [t for t in re.split(r"[\s,，]+", " ".join(raw_tags)) if t]
        if not tags:
            yield event.plain_result(f"要给《{game.get('name_cn') or game['name']}》贴什么标签？例如：/贴标签 名字 魂系 手绘")
            return
        if len(tags) > 5:
            yield event.plain_result("一次最多贴 5 个标签。")
            return

        existing = await self.db.board(group)
        current = next((r for r in existing if r["id"] == game["id"]), game)
        have = [normalize_tag(t) for t in (current.get("custom_tags") or [])]
        if len(have) >= 15:
            yield event.plain_result("这个游戏的标签已经够多了，先这样吧。")
            return

        added: list[str] = []
        skipped: list[str] = []
        for tag in tags:
            tag = tag.strip()[:8]
            norm = normalize_tag(tag)
            if not norm:
                continue
            if norm in have or norm in [normalize_tag(a) for a in added]:
                skipped.append(tag)
                continue
            ok = await self.db.add_custom_tag(group, game["id"], tag, norm)
            (added if ok else skipped).append(tag)

        name = game.get("name_cn") or game["name"]
        parts = []
        if added:
            parts.append(f"已给《{name}》贴上：{'、'.join(added)}")
        if skipped:
            parts.append(f"这几个已经有了：{'、'.join(skipped)}")
        if not added and not skipped:
            parts.append("没贴成功，换个标签试试。")
        parts.append("/榜单 标签名 可以按标签筛选了。")
        yield event.plain_result("\n".join(parts))

    @filter.command("榜名", alias={"boardname", "榜单名"})
    async def cmd_board_title(self, event: AstrMessageEvent):
        """设置本群榜单的标题"""
        uid, _ = self._sender(event)
        if not self._rate_ok(uid):
            yield event.plain_result("操作太频繁了，缓一缓再来。")
            return
        rest = self._rest(event, ["榜名", "boardname", "榜单名"])
        group = self._group(event)
        current = await self.db.get_board_title(group)

        if not rest:
            if current:
                yield event.plain_result(
                    f"本群榜单现在叫「{current}」。\n要改就发 /榜名 新名字，要恢复默认发 /榜名 重置。"
                )
            else:
                yield event.plain_result(
                    "本群榜单还没起名，默认叫「游戏推荐榜」。\n发 /榜名 新名字 就能改。"
                )
            return

        if rest in {"重置", "清除", "默认", "reset", "clear"}:
            await self.db.set_board_title(group, "", uid)
            yield event.plain_result("榜单名已恢复默认：游戏推荐榜。")
            return

        if self._cfg("board_title_admin_only", False) and not self._is_admin(event):
            yield event.plain_result("本群设置只允许管理员改榜单名。")
            return

        title = " ".join(rest.split())[:20]
        if not title:
            yield event.plain_result("名字不能是空的。")
            return
        await self.db.set_board_title(group, title, uid)
        yield event.plain_result(
            f"本群榜单改名为「{title}」，下次 /榜单 就带这个标题。"
            "（想恢复默认发 /榜名 重置）"
        )

    @filter.command("榜单", alias={"board"})
    async def cmd_board(self, event: AstrMessageEvent):
        """输出榜单（可带标签筛选）"""
        rest = self._rest(event, ["榜单", "board"])
        group = self._group(event)
        rows = await self.db.board(group)
        if not rows:
            yield event.plain_result(
                "本群还没有游戏上榜，用 /上榜 游戏名 开个头吧。\n发送 /游戏帮助 可看全部指令。"
            )
            return

        tags_filter, intersect = self._parse_tag_query(rest)
        if tags_filter:
            rows = self._filter_by_tags(rows, tags_filter, intersect)
            if not rows:
                yield event.plain_result(
                    f"没有带「{rest}」标签的游戏。发送 /榜单 可看全部。"
                )
                return

        rows = await self._with_assets(rows)
        base_title = await self.db.get_board_title(group) or "游戏推荐榜"
        title = base_title
        if tags_filter:
            joined = " + ".join(tags_filter) if intersect else " / ".join(tags_filter)
            title = f"{base_title} · {joined}"
        subtitle = f"按均分排序 · 标签筛选：{rest}" if rest else "按均分排序 · 夯 / 顶级 / 人上人 / NPC / 拉完了"
        path = self._image_path("board")
        try:
            await asyncio.to_thread(
                render_board, rows, path, title, subtitle
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[game_board] 渲染榜单失败：{exc}")
            yield event.plain_result("出图失败了，稍后再试。")
            return
        yield event.image_result(str(path))

    @filter.command("小众榜", alias={"小众游戏", "niche"})
    async def cmd_niche(self, event: AstrMessageEvent):
        """打分人数不足的小众游戏"""
        group = self._group(event)
        threshold = int(self._cfg("min_votes_niche", 5))
        rows = await self.db.board(group)
        niche = [r for r in rows if int(r.get("votes") or 0) < threshold]
        if not niche:
            yield event.plain_result(f"目前没有打分人数少于 {threshold} 人的游戏。")
            return
        niche = await self._with_assets(niche)
        base_title = await self.db.get_board_title(group) or "游戏推荐榜"
        path = self._image_path("niche")
        try:
            await asyncio.to_thread(
                render_board,
                niche,
                path,
                f"{base_title} · 小众榜",
                f"打分人数 < {threshold} 人 · 共 {len(niche)} 个",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[game_board] 渲染小众榜失败：{exc}")
            yield event.plain_result("出图失败了，稍后再试。")
            return
        yield event.image_result(str(path))

    @filter.command("详情", alias={"游戏", "game"})
    async def cmd_game(self, event: AstrMessageEvent):
        """查看游戏详情"""
        name = self._rest(event, ["详情", "游戏", "game"])
        if not name:
            yield event.plain_result("用法：/详情 艾尔登法环")
            return
        group = self._group(event)
        board = await self.db.board(group)
        game = self._match(board, name)
        if not game:
            yield event.plain_result(f"榜单里没有「{name}」，先用 /上榜 加进来。")
            return
        stats = await self.db.stats(group, game["id"])
        assert stats is not None
        stats = await self._ensure_assets(stats)
        comments = await self.db.comments(group, game["id"])
        path = self._image_path("detail")
        try:
            await asyncio.to_thread(
                render_detail,
                stats,
                comments,
                path,
                max_comments=int(self._cfg("detail_max_comments", 6)),
                pin_recommender=bool(self._cfg("pin_recommender_comment", True)),
                avatar_dir=self.avatar_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[game_board] 渲染详情失败：{exc}")
            yield event.plain_result("出图失败了，稍后再试。")
            return
        yield event.image_result(str(path))

    # ------------------------------------------------------------------ 辅助

    async def _with_assets(self, rows: list[dict]) -> list[dict]:
        async def prepare(row: dict) -> dict:
            try:
                return await self._ensure_assets(dict(row))
            except Exception:
                return row

        return list(await asyncio.gather(*(prepare(r) for r in rows)))

    def _parse_tag_query(self, rest: str) -> tuple[list[str], bool]:
        if not rest:
            return [], False
        tokens = [t for t in re.split(r"[\s,，]+", rest) if t]
        intersect = any("+" in t for t in tokens)
        tags: list[str] = []
        for token in tokens:
            tags.extend([part for part in token.split("+") if part])
        return tags, intersect

    def _filter_by_tags(
        self, rows: list[dict], tags: list[str], intersect: bool
    ) -> list[dict]:
        wanted = [normalize_tag(t) for t in tags if normalize_tag(t)]
        if not wanted:
            return rows

        def row_tags(row: dict) -> set[str]:
            all_tags = list(row.get("tags") or []) + list(row.get("custom_tags") or [])
            return {normalize_tag(t) for t in all_tags if t}

        if intersect:
            return [r for r in rows if all(t in row_tags(r) for t in wanted)]
        return [r for r in rows if any(t in row_tags(r) for t in wanted)]

    def _split_game_and_tags(
        self, rows: list[dict], rest: str
    ) -> tuple[dict | None, list[str]]:
        """从「游戏名 标签1 标签2」里切出游戏。

        名字可能带空格，所以从长到短试，但**只认精确匹配**——
        用模糊匹配会把「空洞骑士 魂系」整体当成游戏名，把标签吃掉。
        """
        tokens = rest.split()
        for k in range(len(tokens) - 1, 0, -1):
            query = " ".join(tokens[:k])
            game = self._exact_match(rows, query)
            if game:
                return game, tokens[k:]
        game = self._match(rows, tokens[0]) if tokens else None
        if game:
            return game, tokens[1:]
        return None, []

    def _exact_match(self, rows: list[dict], query: str) -> dict | None:
        q = normalize_name(query)
        if not q:
            return None
        for row in rows:
            if q in {
                normalize_name(row.get("name") or ""),
                normalize_name(row.get("name_cn") or ""),
            }:
                return row
        return None

    def _match(self, rows: list[dict], query: str) -> dict | None:
        q = normalize_name(query)
        if not q:
            return None
        for row in rows:
            if q in {normalize_name(row.get("name") or ""), normalize_name(row.get("name_cn") or "")}:
                return row
        for row in rows:
            if q in normalize_name(row.get("name") or "") or q in normalize_name(
                row.get("name_cn") or ""
            ):
                return row
        mapping = {
            normalize_name(row.get("name_cn") or row.get("name") or ""): row for row in rows
        }
        best = difflib.get_close_matches(q, list(mapping.keys()), n=1, cutoff=0.6)
        return mapping[best[0]] if best else None

    async def terminate(self):
        try:
            self.db.close()
        except Exception:
            pass
        for directory in (self.image_dir,):
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except Exception:
                pass
