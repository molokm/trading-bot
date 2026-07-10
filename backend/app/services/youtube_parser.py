"""YouTube channel parser — fetches video descriptions and subtitles from @AlexFalcony.

Primary: yt-dlp (if available). Fallback: HTTP (RSS feed + page scrape).
"""

import re
import asyncio
import json
import shutil
import os
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class YouTubeVideo:
    channel: str
    video_id: str
    title: str
    description: str
    timestamp: Optional[datetime] = None
    url: str = ""
    duration_seconds: int = 0
    view_count: int = 0
    subtitles_text: str = ""


def _has_ytdlp() -> bool:
    """Check if yt-dlp binary is available."""
    import sys
    venv_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.exists(venv_bin):
        return True
    return shutil.which("yt-dlp") is not None


def _ytdlp_path() -> str:
    import sys
    venv_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.exists(venv_bin):
        return venv_bin
    return shutil.which("yt-dlp") or "yt-dlp"


class YouTubeParser:
    """Fetches video metadata, descriptions and subtitles from a YouTube channel."""

    def __init__(self, channel_handle: str):
        self.channel_handle = channel_handle
        self.channel_url = f"https://www.youtube.com/@{channel_handle}"
        self._known_ids: set = set()
        self._use_ytdlp = _has_ytdlp()
        self._use_rss = not self._use_ytdlp

    async def fetch_recent_videos(self, limit: int = 10) -> List[YouTubeVideo]:
        if self._use_ytdlp:
            return await self._fetch_via_ytdlp(limit)
        return await self._fetch_via_rss(limit)

    async def _fetch_via_ytdlp(self, limit: int) -> List[YouTubeVideo]:
        """Fetch using yt-dlp (preferred)."""
        cmd = [
            _ytdlp_path(),
            "--flat-playlist",
            "--dump-json",
            "--playlist-end", str(limit),
            self.channel_url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except Exception as e:
            print(f"[YT] yt-dlp failed: {e}", flush=True)
            self._use_ytdlp = False
            self._use_rss = True
            return await self._fetch_via_rss(limit)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            print(f"[YT] yt-dlp error (code {proc.returncode}): {err[:200]}", flush=True)
            self._use_ytdlp = False
            self._use_rss = True
            return await self._fetch_via_rss(limit)

        videos = []
        for line in stdout.decode(errors="replace").strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            vid = data.get("id", "")
            if vid in self._known_ids:
                continue
            self._known_ids.add(vid)

            ts = None
            if data.get("timestamp"):
                try:
                    ts = datetime.utcfromtimestamp(data["timestamp"])
                except Exception:
                    pass

            videos.append(YouTubeVideo(
                channel=self.channel_handle,
                video_id=vid,
                title=data.get("title", ""),
                description=data.get("description", ""),
                timestamp=ts,
                url=f"https://youtu.be/{vid}",
                duration_seconds=data.get("duration") or 0,
                view_count=data.get("view_count") or 0,
            ))

        return videos

    async def _fetch_via_rss(self, limit: int) -> List[YouTubeVideo]:
        """Fallback: fetch via YouTube RSS feed + page scrape (no yt-dlp needed)."""
        import httpx

        # Step 1: RSS feed for recent video IDs and titles
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id=placeholder"
        # We need channel_id — fetch from page
        channel_id = await self._get_channel_id()
        if not channel_id:
            print("[YT] Could not get channel_id, trying direct RSS", flush=True)
            return []

        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        videos = []

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(rss_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    print(f"[YT] RSS returned {resp.status_code}", flush=True)
                    return []

                xml_text = resp.text
                # Parse entries from Atom feed
                entries = re.findall(r'<entry>(.*?)</entry>', xml_text, re.DOTALL)

                for entry in entries[:limit]:
                    vid_match = re.search(r'<yt:videoId>(.*?)</yt:videoId>', entry)
                    title_match = re.search(r'<title>(.*?)</title>', entry)
                    pub_match = re.search(r'<published>(.*?)</published>', entry)

                    if not vid_match:
                        continue

                    vid = vid_match.group(1)
                    if vid in self._known_ids:
                        continue
                    self._known_ids.add(vid)

                    title = title_match.group(1) if title_match else ""
                    # Unescape XML entities
                    title = title.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')

                    ts = None
                    if pub_match:
                        try:
                            ts = datetime.fromisoformat(pub_match.group(1).replace("Z", "+00:00"))
                        except Exception:
                            pass

                    videos.append(YouTubeVideo(
                        channel=self.channel_handle,
                        video_id=vid,
                        title=title,
                        description="",
                        timestamp=ts,
                        url=f"https://youtu.be/{vid}",
                    ))
        except Exception as e:
            print(f"[YT] RSS fetch error: {e}", flush=True)

        # Step 2: Scrape video pages for descriptions
        if videos:
            for v in videos[:min(3, len(videos))]:
                await self._scrape_video_page(v)

        return videos

    async def _get_channel_id(self) -> Optional[str]:
        """Get YouTube channel ID from the channel page."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    self.channel_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                if resp.status_code != 200:
                    return None
                match = re.search(r'"externalId":"(UC[^"]+)"', resp.text)
                if match:
                    return match.group(1)
                match = re.search(r'channel_id=(UC[^"&]+)', resp.text)
                if match:
                    return match.group(1)
        except Exception as e:
            print(f"[YT] channel_id error: {e}", flush=True)
        return None

    async def _scrape_video_page(self, video: YouTubeVideo):
        """Scrape video page for description and basic info."""
        import httpx
        try:
            url = f"https://www.youtube.com/watch?v={video.video_id}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                if resp.status_code != 200:
                    return
                html = resp.text

                # Extract description from meta or structured data
                desc_match = re.search(r'"shortDescription":"(.*?)"', html)
                if desc_match:
                    desc = desc_match.group(1)
                    desc = desc.replace("\\n", "\n").replace("\\u0026", "&")
                    video.description = desc

                # Extract view count
                views_match = re.search(r'"viewCount":"(\d+)"', html)
                if views_match:
                    video.view_count = int(views_match.group(1))

                # Extract duration
                dur_match = re.search(r'"lengthSeconds":"(\d+)"', html)
                if dur_match:
                    video.duration_seconds = int(dur_match.group(1))
        except Exception as e:
            print(f"[YT] page scrape error for {video.video_id}: {e}", flush=True)

    async def fetch_subtitles(self, video_id: str) -> str:
        """Fetch subtitles — yt-dlp if available, else empty (subtitles are bonus)."""
        if not self._use_ytdlp:
            return ""
        cmd = [
            _ytdlp_path(),
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang", "ru,en",
            "--sub-format", "vtt",
            "--output", "/tmp/yt_sub_%(id)s",
            f"https://youtu.be/{video_id}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            return ""

        import glob
        import re as _re
        pattern = f"/tmp/yt_sub_{video_id}*.vtt"
        files = glob.glob(pattern)
        if not files:
            return ""

        text_parts = []
        for fpath in sorted(files):
            with open(fpath, "r", errors="replace") as f:
                for line in f:
                    if line.startswith("WEBVTT") or line.strip() == "" or "-->" in line:
                        continue
                    clean = _re.sub(r"<[^>]+>", "", line).strip()
                    if clean and clean not in text_parts[-1:]:
                        text_parts.append(clean)
            os.remove(fpath)

        return " ".join(text_parts)


async def test_youtube_parser():
    """Test the YouTube parser on @AlexFalcony."""
    parser = YouTubeParser("AlexFalcony")
    print(f"yt-dlp available: {parser._use_ytdlp}")
    print(f"Using: {'yt-dlp' if parser._use_ytdlp else 'RSS+HTTP'}")

    videos = await parser.fetch_recent_videos(limit=5)
    print(f"Fetched {len(videos)} videos from @{parser.channel_handle}\n")

    for v in videos[:3]:
        ts = v.timestamp.strftime("%Y-%m-%d") if v.timestamp else "?"
        print(f"[{ts}] {v.title}")
        print(f"  URL: {v.url}  views: {v.view_count}")
        print(f"  Description: {v.description[:200]}")
        print()


if __name__ == "__main__":
    asyncio.run(test_youtube_parser())
