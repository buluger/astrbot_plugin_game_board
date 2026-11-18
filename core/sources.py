"""游戏信息数据源：Steam / Bangumi / 手动。

对外统一返回 Candidate（搜索候选）与 GameInfo（详情），
下载封面时统一落到插件数据目录的 covers/ 下。
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

UA = "Mozilla/5.0 (compatible; astrbot-plugin-game-board/0.1)"
TIMEOUT = aiohttp.ClientTimeout(total=15)


@dataclass
class Candidate:
    source: str
    source_id: str
    name: str
    name_cn: str = ""
    image: str = ""
    detail: str = ""


@dataclass
class GameInfo:
    source: str
    source_id: str
    name: str
    name_cn: str = ""
    tags: list[str] = field(default_factory=list)
    desc: str = ""
    cover_url: str = ""
    header_url: str = ""
    header_fallback: str = ""


async def _get_json(session: aiohttp.ClientSession, url: str, **kwargs):
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _get_text(session: aiohttp.ClientSession, url: str, **kwargs) -> str:
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.text(errors="ignore")


class SteamSource:
    name = "steam"
    _SEARCH = "https://store.steampowered.com/api/storesearch/"
    _DETAIL = "https://store.steampowered.com/api/appdetails"

    def __init__(self, language: str = "schinese", country: str = "cn"):
        self.language = language
        self.country = country

    async def search(self, term: str, limit: int = 6) -> list[Candidate]:
        params = {"term": term, "l": self.language, "cc": self.country}
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
            data = await _get_json(s, self._SEARCH, params=params)
        items = (data or {}).get("items") or []
        out: list[Candidate] = []
        for item in items[:limit]:
            if item.get("type") != "app":
                continue
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(item.get("id")),
                    name=item.get("name", ""),
                    image=item.get("tiny_image", ""),
                )
            )
        return out

    async def user_tags(self, appid: str, limit: int = 8) -> list[str]:
        """抓商店页的「热门用户自定义标签」。

        appdetails 接口只给 genres / categories（单人、多人、动作这类），
        真正的玩家标签在商店页 HTML 的 app_tag 里，对游戏分类准得多。
        """
        url = f"https://store.steampowered.com/app/{appid}/"
        params = {"l": self.language, "cc": self.country}
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
                html = await _get_text(s, url, params=params)
        except Exception:
            return []
        raw = re.findall(r'class="app_tag"[^>]*>\s*([^<]+?)\s*</a>', html)
        out: list[str] = []
        for tag in raw:
            tag = tag.strip()
            if not tag or tag in {"+", "-"} or tag in out:
                continue
            out.append(tag)
        return out[:limit]

    async def detail(self, appid: str) -> GameInfo | None:
        params = {"appids": appid, "l": self.language, "cc": self.country}
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
            data = await _get_json(s, self._DETAIL, params=params)
        payload = (data or {}).get(str(appid)) or {}
        if not payload.get("success"):
            return None
        d = payload["data"]
        tags = await self.user_tags(appid)
        if not tags:
            # 锁区 / 年龄验证页拿不到标签时，退回 genres + categories
            tags = [g.get("description", "") for g in d.get("genres", [])]
            tags += [c.get("description", "") for c in d.get("categories", [])][:3]
        cdn = "https://cdn.akamai.steamstatic.com/steam/apps"
        header_fallback = d.get("header_image", "") or ""
        return GameInfo(
            source=self.name,
            source_id=str(appid),
            name=d.get("name", ""),
            name_cn=d.get("name", ""),
            tags=[t for t in tags if t],
            desc=d.get("short_description", "") or "",
            # 卡片用 header.jpg（460x215，比例和榜单卡片一致），详情页用 library_hero
            cover_url=header_fallback or f"{cdn}/{appid}/header.jpg",
            header_url=f"{cdn}/{appid}/library_hero.jpg",
            header_fallback=header_fallback,
        )


class BangumiSource:
    name = "bangumi"
    _SEARCH = "https://api.bgm.tv/v0/search/subjects"
    _SUBJECT = "https://api.bgm.tv/v0/subjects/"

    async def search(self, term: str, limit: int = 6) -> list[Candidate]:
        body = {"keyword": term, "filter": {"type": [4]}}  # type 4 = 游戏
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
            async with s.post(self._SEARCH, params={"limit": limit}, json=body) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        out: list[Candidate] = []
        for item in (data or {}).get("data", [])[:limit]:
            images = item.get("images") or {}
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(item.get("id")),
                    name=item.get("name", ""),
                    name_cn=item.get("name_cn", "") or "",
                    image=images.get("large", "") or images.get("common", ""),
                    detail=item.get("date", "") or "",
                )
            )
        return out

    async def detail(self, subject_id: str) -> GameInfo | None:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
            data = await _get_json(s, f"{self._SUBJECT}{subject_id}")
        if not data:
            return None
        tags = [t.get("name", "") for t in (data.get("tags") or [])][:8]
        images = data.get("images") or {}
        return GameInfo(
            source=self.name,
            source_id=str(subject_id),
            name=data.get("name", ""),
            name_cn=data.get("name_cn", "") or "",
            tags=[t for t in tags if t],
            desc=(data.get("summary", "") or "").strip()[:300],
            cover_url=images.get("large", "") or images.get("common", ""),
            header_url=images.get("large", "") or images.get("common", ""),
        )


def safe_filename(text: str, fallback: str = "cover") -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return (text or fallback)[:80]


async def download(url: str, dest: Path) -> bool:
    """下载图片到本地，已存在则直接返回 True。"""
    if not url:
        return False
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
            async with s.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
        if len(data) < 512:
            return False
        await asyncio.to_thread(dest.write_bytes, data)
        return True
    except Exception:
        return False
