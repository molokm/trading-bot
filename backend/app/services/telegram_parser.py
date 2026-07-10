"""Telegram channel parser — scrapes posts from public channels via t.me/s/ preview."""

import re
import asyncio
from datetime import datetime, timezone
from typing import List, Optional
from dataclasses import dataclass, field

import aiohttp
from bs4 import BeautifulSoup


@dataclass
class TelegramPost:
    channel: str
    text: str
    timestamp: Optional[datetime] = None
    post_url: str = ""
    views: int = 0
    has_media: bool = False
    media_type: str = ""  # "photo", "video", "youtube"
    youtube_url: str = ""


class TelegramParser:
    """Scrapes public Telegram channels via the t.me/s/ web preview."""

    BASE_URL = "https://t.me/s/{channel}"

    def __init__(self, channel: str):
        self.channel = channel
        self.url = self.BASE_URL.format(channel=channel)
        self._last_post_id = 0

    async def fetch_posts(self, limit: int = 50) -> List[TelegramPost]:
        """Fetch recent posts from the channel preview."""
        async with aiohttp.ClientSession() as session:
            async with session.get(self.url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to fetch {self.url}: {resp.status}")
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        posts = []

        for widget in soup.select(".tgme_widget_message_wrap"):
            try:
                post = self._parse_widget(widget)
                if post and (not self._last_post_id or
                             post.post_url != self._last_post_id):
                    posts.append(post)
            except Exception:
                continue

        if posts:
            self._last_post_id = posts[0].post_url

        return posts[:limit]

    def _parse_widget(self, widget) -> Optional[TelegramPost]:
        text_div = widget.select_one(".tgme_widget_message_text")
        if not text_div:
            return None

        text = text_div.get_text(separator="\n").strip()
        if not text:
            return None

        # Extract timestamp
        time_tag = widget.select_one("time")
        timestamp = None
        if time_tag and time_tag.get("datetime"):
            try:
                timestamp = datetime.fromisoformat(
                    time_tag["datetime"].replace("Z", "+00:00")
                )
            except Exception:
                pass

        # Extract post URL
        post_link = widget.select_one(".tgme_widget_message_date")
        post_url = ""
        if post_link and post_link.get("href"):
            post_url = post_link["href"]

        # Extract views
        views = 0
        views_span = widget.select_one(".tgme_widget_message_views")
        if views_span:
            views_text = views_span.get_text().strip()
            views = self._parse_views(views_text)

        # Check for media
        has_media = bool(widget.select_one(".tgme_widget_message_photo"))
        media_type = "photo" if has_media else ""

        # Check for YouTube link
        youtube_url = ""
        for a_tag in widget.select("a"):
            href = a_tag.get("href", "")
            if "youtube.com" in href or "youtu.be" in href:
                youtube_url = href
                media_type = "youtube"
                break

        return TelegramPost(
            channel=self.channel,
            text=text,
            timestamp=timestamp,
            post_url=post_url,
            views=views,
            has_media=has_media,
            media_type=media_type,
            youtube_url=youtube_url,
        )

    @staticmethod
    def _parse_views(text: str) -> int:
        text = text.strip().lower()
        if "k" in text:
            return int(float(text.replace("k", "")) * 1000)
        if "m" in text:
            return int(float(text.replace("m", "")) * 1000000)
        try:
            return int(text)
        except ValueError:
            return 0


async def test_parser():
    """Test the parser on @falconinvestors."""
    parser = TelegramParser("falconinvestors")
    posts = await parser.fetch_posts(limit=10)
    print(f"Fetched {len(posts)} posts from @{parser.channel}\n")
    for p in posts[:5]:
        ts = p.timestamp.strftime("%Y-%m-%d %H:%M") if p.timestamp else "?"
        print(f"[{ts}] views={p.views} media={p.media_type or 'none'}")
        print(f"  {p.text[:200]}")
        if p.youtube_url:
            print(f"  YouTube: {p.youtube_url}")
        print()


if __name__ == "__main__":
    asyncio.run(test_parser())
