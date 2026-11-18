"""按 Steam / Bangumi 重新拉一遍标签，更新已有游戏的标签。

群友用 /贴标签 加的自定义标签存在 custom_tags 表，这里只动 games.tags，不会被覆盖。

用法：
    python scripts/refresh_tags.py --db /opt/AstrBot/data/plugin_data/game_board/board.db
    python scripts/refresh_tags.py --db ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent))

from astrbot_plugin_game_board.core.db import GameStore
from astrbot_plugin_game_board.core.sources import BangumiSource, SteamSource


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    store = GameStore(db_path.parent)
    steam, bangumi = SteamSource(), BangumiSource()
    rows = await store.query(
        "SELECT id, source, source_id, name, name_cn, tags FROM games "
        "WHERE source IN ('steam', 'bangumi') ORDER BY id"
    )
    print(f"共 {len(rows)} 个游戏待检查")
    changed = failed = 0
    for i, row in enumerate(rows, 1):
        try:
            if row["source"] == "steam":
                tags = await steam.user_tags(row["source_id"])
            else:
                # Bangumi 标签越往后越是「游戏 / PC / PS5」这类噪音，取前 6 个足够
                info = await bangumi.detail(row["source_id"])
                tags = (info.tags[:6] if info else [])
        except Exception as exc:  # noqa: BLE001
            print(f"  [{row['id']}] {row['name']} 拉取失败：{exc}")
            failed += 1
            continue
        if not tags:
            print(f"  [{row['id']}] {row['name']} 没抓到标签，保持原样")
            failed += 1
            continue
        if tags != row["tags"]:
            changed += 1
            print(f"  [{row['id']}] {row['name_cn'] or row['name']}")
            print(f"      旧：{row['tags']}")
            print(f"      新：{tags}")
            if not args.dry_run:
                await store.update_tags(row["id"], tags)
        await asyncio.sleep(args.sleep)
    print(f"完成：更新 {changed} 个，失败 {failed} 个" + ("（dry-run 未写库）" if args.dry_run else ""))


if __name__ == "__main__":
    asyncio.run(main())
