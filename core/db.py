"""SQLite 数据层。

设计要点：
- games 全局共享（Steam / Bangumi 来源），manual 来源按群隔离；
- ratings / comments / submissions 按 group_id 隔离；
- 所有写操作走同一把线程锁，读操作交给线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,             -- steam | bangumi | manual
    source_id      TEXT NOT NULL,             -- appid / bgm id / manual:<uuid>
    name           TEXT NOT NULL,             -- 原名
    name_cn        TEXT NOT NULL DEFAULT '',  -- 中文名
    name_norm      TEXT NOT NULL,             -- 归一化名，用于去重与匹配
    cover_path     TEXT NOT NULL DEFAULT '',  -- 榜单卡片封面（本地缓存）
    header_path    TEXT NOT NULL DEFAULT '',  -- 详情页大图（本地缓存）
    cover_url      TEXT NOT NULL DEFAULT '',
    header_url     TEXT NOT NULL DEFAULT '',
    tags           TEXT NOT NULL DEFAULT '[]',-- JSON 数组，自动抓取的标签
    desc           TEXT NOT NULL DEFAULT '',
    manual_group   TEXT NOT NULL DEFAULT '',  -- manual 游戏所属群，其他来源为空
    created_at     INTEGER NOT NULL,
    created_by     TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_games_source
    ON games(source, source_id) WHERE source != 'manual';
CREATE INDEX IF NOT EXISTS idx_games_norm ON games(name_norm);

CREATE TABLE IF NOT EXISTS submissions (
    group_id   TEXT NOT NULL,
    game_id    INTEGER NOT NULL,
    user_id    TEXT NOT NULL,
    comment    TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, game_id)
);

CREATE TABLE IF NOT EXISTS ratings (
    group_id   TEXT NOT NULL,
    game_id    INTEGER NOT NULL,
    user_id    TEXT NOT NULL,
    score      REAL NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    avatar     TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, game_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ratings_game ON ratings(group_id, game_id);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL,
    game_id    INTEGER NOT NULL,
    user_id    TEXT NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    avatar     TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_game ON comments(group_id, game_id);

CREATE TABLE IF NOT EXISTS group_settings (
    group_id    TEXT PRIMARY KEY,
    board_title TEXT NOT NULL DEFAULT '',   -- 本群自定义榜单标题，空串表示用默认
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS custom_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL,
    game_id    INTEGER NOT NULL,
    tag        TEXT NOT NULL,
    tag_norm   TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (group_id, game_id, tag_norm)
);
"""


class GameStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "board.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 低层工具 ----------

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return await asyncio.to_thread(self._query, sql, params)

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await asyncio.to_thread(self._execute, sql, params)

    # ---------- 游戏 ----------

    async def find_game(self, source: str, source_id: str) -> dict | None:
        rows = await self.query(
            "SELECT * FROM games WHERE source = ? AND source_id = ?",
            (source, source_id),
        )
        return rows[0] if rows else None

    async def add_game(
        self,
        *,
        source: str,
        source_id: str,
        name: str,
        name_cn: str = "",
        name_norm: str,
        tags: list[str] | None = None,
        desc: str = "",
        cover_path: str = "",
        header_path: str = "",
        cover_url: str = "",
        header_url: str = "",
        manual_group: str = "",
        created_by: str = "",
    ) -> int:
        cur = await asyncio.to_thread(
            self._execute,
            """INSERT INTO games
               (source, source_id, name, name_cn, name_norm, tags, desc,
                cover_path, header_path, cover_url, header_url,
                manual_group, created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source,
                source_id,
                name,
                name_cn,
                name_norm,
                json.dumps(tags or [], ensure_ascii=False),
                desc,
                cover_path,
                header_path,
                cover_url,
                header_url,
                manual_group,
                int(time.time()),
                created_by,
            ),
        )
        return int(cur.lastrowid)

    async def update_game_assets(
        self,
        game_id: int,
        *,
        cover_path: str = "",
        header_path: str = "",
        cover_url: str = "",
        header_url: str = "",
        tags: list[str] | None = None,
        desc: str = "",
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if cover_path:
            fields.append("cover_path = ?")
            params.append(cover_path)
        if header_path:
            fields.append("header_path = ?")
            params.append(header_path)
        if cover_url:
            fields.append("cover_url = ?")
            params.append(cover_url)
        if header_url:
            fields.append("header_url = ?")
            params.append(header_url)
        if tags:
            fields.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if desc:
            fields.append("desc = ?")
            params.append(desc)
        if not fields:
            return
        params.append(game_id)
        await self.execute(f"UPDATE games SET {', '.join(fields)} WHERE id = ?", tuple(params))

    async def get_game(self, game_id: int) -> dict | None:
        rows = await self.query("SELECT * FROM games WHERE id = ?", (game_id,))
        return rows[0] if rows else None

    # ---------- 榜单 ----------

    async def board(self, group_id: str) -> list[dict]:
        """本群榜单：只包含在本群被推荐过的游戏，附带均分、票数、自定义标签。"""
        rows = await self.query(
            """
            SELECT g.*,
                   COALESCE(AVG(r.score), 0) AS avg_score,
                   COUNT(r.user_id) AS votes,
                   s.user_id AS recommender,
                   s.comment AS recommend_comment
            FROM games g
            JOIN submissions s ON s.game_id = g.id AND s.group_id = ?
            LEFT JOIN ratings r ON r.game_id = g.id AND r.group_id = ?
            WHERE g.manual_group = '' OR g.manual_group = ?
            GROUP BY g.id
            ORDER BY avg_score DESC
            """,
            (group_id, group_id, group_id),
        )
        for row in rows:
            row["tags"] = json.loads(row.get("tags") or "[]")
            row["custom_tags"] = await asyncio.to_thread(
                self._custom_tags_sync, group_id, row["id"]
            )
        return rows

    def _custom_tags_sync(self, group_id: str, game_id: int) -> list[str]:
        rows = self._query(
            "SELECT tag FROM custom_tags WHERE group_id = ? AND game_id = ?",
            (group_id, game_id),
        )
        return [r["tag"] for r in rows]

    async def stats(self, group_id: str, game_id: int) -> dict | None:
        rows = await self.query(
            """
            SELECT g.*, COALESCE(AVG(r.score), 0) AS avg_score, COUNT(r.user_id) AS votes
            FROM games g
            LEFT JOIN ratings r ON r.game_id = g.id AND r.group_id = ?
            WHERE g.id = ?
            GROUP BY g.id
            """,
            (group_id, game_id),
        )
        if not rows:
            return None
        row = rows[0]
        row["tags"] = json.loads(row.get("tags") or "[]")
        row["custom_tags"] = await asyncio.to_thread(
            self._custom_tags_sync, group_id, game_id
        )
        return row

    # ---------- 推荐 / 评分 / 评论 ----------

    async def add_submission(
        self, group_id: str, game_id: int, user_id: str, comment: str = ""
    ) -> None:
        await self.execute(
            """INSERT INTO submissions (group_id, game_id, user_id, comment, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(group_id, game_id) DO UPDATE SET
                 comment = CASE WHEN excluded.comment != '' THEN excluded.comment ELSE submissions.comment END""",
            (group_id, game_id, user_id, comment, int(time.time())),
        )

    async def has_submission(self, group_id: str, game_id: int) -> dict | None:
        rows = await self.query(
            "SELECT * FROM submissions WHERE group_id = ? AND game_id = ?",
            (group_id, game_id),
        )
        return rows[0] if rows else None

    async def set_rating(
        self,
        group_id: str,
        game_id: int,
        user_id: str,
        score: float,
        nickname: str = "",
        avatar: str = "",
    ) -> None:
        await self.execute(
            """INSERT INTO ratings (group_id, game_id, user_id, score, nickname, avatar, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(group_id, game_id, user_id) DO UPDATE SET
                 score = excluded.score,
                 nickname = excluded.nickname,
                 avatar = excluded.avatar,
                 updated_at = excluded.updated_at""",
            (group_id, game_id, user_id, score, nickname, avatar, int(time.time())),
        )

    async def del_rating(self, group_id: str, game_id: int, user_id: str) -> bool:
        rows = await self.query(
            "SELECT 1 AS x FROM ratings WHERE group_id=? AND game_id=? AND user_id=?",
            (group_id, game_id, user_id),
        )
        if not rows:
            return False
        await self.execute(
            "DELETE FROM ratings WHERE group_id=? AND game_id=? AND user_id=?",
            (group_id, game_id, user_id),
        )
        return True

    async def ratings_list(self, group_id: str, game_id: int) -> list[dict]:
        return await self.query(
            "SELECT * FROM ratings WHERE group_id = ? AND game_id = ? "
            "ORDER BY score DESC, updated_at ASC",
            (group_id, game_id),
        )

    async def my_rating(self, group_id: str, game_id: int, user_id: str) -> float | None:
        rows = await self.query(
            "SELECT score FROM ratings WHERE group_id=? AND game_id=? AND user_id=?",
            (group_id, game_id, user_id),
        )
        return float(rows[0]["score"]) if rows else None

    async def add_comment(
        self,
        group_id: str,
        game_id: int,
        user_id: str,
        text: str,
        nickname: str = "",
        avatar: str = "",
        pinned: bool = False,
    ) -> int:
        cur = await asyncio.to_thread(
            self._execute,
            """INSERT INTO comments
               (group_id, game_id, user_id, nickname, avatar, text, pinned, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                group_id,
                game_id,
                user_id,
                nickname,
                avatar,
                text,
                1 if pinned else 0,
                int(time.time()),
            ),
        )
        return int(cur.lastrowid)

    async def comments(self, group_id: str, game_id: int) -> list[dict]:
        return await self.query(
            """SELECT * FROM comments WHERE group_id = ? AND game_id = ?
               ORDER BY pinned DESC, created_at ASC, id ASC""",
            (group_id, game_id),
        )

    async def count_comment_of_user(self, group_id: str, game_id: int, user_id: str) -> int:
        rows = await self.query(
            "SELECT COUNT(*) AS n FROM comments WHERE group_id=? AND game_id=? AND user_id=?",
            (group_id, game_id, user_id),
        )
        return int(rows[0]["n"]) if rows else 0

    async def delete_game_from_group(self, group_id: str, game_id: int) -> None:
        await self.execute(
            "DELETE FROM comments WHERE group_id=? AND game_id=?", (group_id, game_id)
        )
        await self.execute(
            "DELETE FROM ratings WHERE group_id=? AND game_id=?", (group_id, game_id)
        )
        await self.execute(
            "DELETE FROM custom_tags WHERE group_id=? AND game_id=?", (group_id, game_id)
        )
        await self.execute(
            "DELETE FROM submissions WHERE group_id=? AND game_id=?", (group_id, game_id)
        )

    # ---------- 群设置 ----------

    async def update_tags(self, game_id: int, tags: list[str]) -> None:
        """只更新游戏库里的标签，群自定义标签在另一张表，不受影响。"""
        await self.execute(
            "UPDATE games SET tags = ? WHERE id = ?",
            (json.dumps(tags, ensure_ascii=False), game_id),
        )

    async def get_board_title(self, group_id: str) -> str:
        rows = await self.query(
            "SELECT board_title FROM group_settings WHERE group_id = ?", (group_id,)
        )
        return (rows[0]["board_title"] or "") if rows else ""

    async def set_board_title(self, group_id: str, title: str, user_id: str) -> None:
        await self.execute(
            """
            INSERT INTO group_settings (group_id, board_title, updated_at, updated_by)
            VALUES (?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET
                board_title = excluded.board_title,
                updated_at  = excluded.updated_at,
                updated_by  = excluded.updated_by
            """,
            (group_id, title, int(time.time()), user_id),
        )

    async def add_custom_tag(self, group_id: str, game_id: int, tag: str, tag_norm: str) -> bool:
        try:
            await self.execute(
                "INSERT INTO custom_tags (group_id, game_id, tag, tag_norm, created_at) VALUES (?,?,?,?,?)",
                (group_id, game_id, tag, tag_norm, int(time.time())),
            )
            return True
        except sqlite3.IntegrityError:
            return False
