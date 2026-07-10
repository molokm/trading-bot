"""YouTube channel parser — fetches video descriptions and subtitles from @AlexFalcony."""

import re
import asyncio
import json
import shutil
import subprocess
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass


def _yt_dlp_path() -> str:
    """Find yt-dlp binary — venv first, then system."""
    import sys
    import os
    # Check if running inside a venv
    venv_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.exists(venv_bin):
        return venv_bin
    venv_path = shutil.which("yt-dlp")
    if venv_path:
        return venv_path
    return "yt-dlp"


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


class YouTubeParser:
    """Fetches video metadata, descriptions and subtitles from a YouTube channel."""

    def __init__(self, channel_handle: str):
        self.channel_handle = channel_handle
        self.channel_url = f"https://www.youtube.com/@{channel_handle}"
        self._known_ids: set = set()

    async def fetch_recent_videos(self, limit: int = 10) -> List[YouTubeVideo]:
        """Fetch recent videos from the channel."""
        cmd = [
            _yt_dlp_path(),
            "--flat-playlist",
            "--dump-json",
            "--playlist-end", str(limit),
            self.channel_url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"yt-dlp failed: {err}")

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

    async def fetch_subtitles(self, video_id: str) -> str:
        """Fetch auto-generated or manual subtitles for a video."""
        url = f"https://youtu.be/{video_id}"
        cmd = [
            _yt_dlp_path(),
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang", "ru,en",
            "--sub-format", "vtt",
            "--output", "/tmp/yt_sub_%(id)s",
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)

        # Try to find the subtitle file
        import glob
        pattern = f"/tmp/yt_sub_{video_id}*.vtt"
        files = glob.glob(pattern)
        if not files:
            return ""

        text_parts = []
        for fpath in sorted(files):
            with open(fpath, "r", errors="replace") as f:
                for line in f:
                    # Skip VTT headers and timestamps
                    if line.startswith("WEBVTT") or line.strip() == "" or "-->" in line:
                        continue
                    # Remove VTT tags like <c>, </c>
                    clean = re.sub(r"<[^>]+>", "", line).strip()
                    if clean and clean not in text_parts[-1:]:
                        text_parts.append(clean)
            import os
            os.remove(fpath)

        return " ".join(text_parts)

    async def fetch_video_with_subs(self, video_id: str) -> YouTubeVideo:
        """Fetch full video info including subtitles."""
        url = f"https://youtu.be/{video_id}"
        cmd = [
            _yt_dlp_path(),
            "--dump-json",
            "--skip-download",
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)

        data = json.loads(stdout.decode(errors="replace"))
        ts = None
        if data.get("timestamp"):
            try:
                ts = datetime.utcfromtimestamp(data["timestamp"])
            except Exception:
                pass

        video = YouTubeVideo(
            channel=self.channel_handle,
            video_id=video_id,
            title=data.get("title", ""),
            description=data.get("description", ""),
            timestamp=ts,
            url=url,
            duration_seconds=data.get("duration") or 0,
            view_count=data.get("view_count") or 0,
        )

        video.subtitles_text = await self.fetch_subtitles(video_id)
        return video


async def test_youtube_parser():
    """Test the YouTube parser on @AlexFalcony."""
    parser = YouTubeParser("AlexFalcony")
    videos = await parser.fetch_recent_videos(limit=5)
    print(f"Fetched {len(videos)} videos from @{parser.channel_handle}\n")

    for v in videos[:3]:
        ts = v.timestamp.strftime("%Y-%m-%d") if v.timestamp else "?"
        print(f"[{ts}] {v.title}")
        print(f"  URL: {v.url}  views: {v.view_count}")
        print(f"  Description: {v.description[:300]}")
        print()

    # Fetch subtitles for the most recent video
    if videos:
        print(f"\n=== Subtitles for: {videos[0].title} ===")
        subs = await parser.fetch_subtitles(videos[0].video_id)
        print(f"Subtitles ({len(subs)} chars): {subs[:500]}...")


if __name__ == "__main__":
    asyncio.run(test_youtube_parser())
