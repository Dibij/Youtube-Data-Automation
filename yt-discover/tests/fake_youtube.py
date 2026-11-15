"""In-memory fake YouTube Data API for tests. No network is used."""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yt_discover import youtube as yt  # noqa: E402


def make_video(video_id: str, *, title: str = "A video", channel_id: str = "UCchan000001",
               channel_title: str = "Channel", published_ts: float = 1700000000.0,
               duration: int | None = 600, views: int | None = 10000,
               tags=None, live: str = "none", category: str = "27") -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "published_at": "2023-11-14T00:00:00Z",
        "published_ts": published_ts,
        "duration_seconds": duration,
        "views": views,
        "tags": list(tags or []),
        "category_id": category,
        "live_broadcast": live,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
    }


class FakeYouTube:
    """Mimics the two methods the app uses, including quota units counters.

    `search_results` maps a topic substring -> list of video dicts.  Each call
    rotates through the configured results (optionally paged) so repeated
    searches return fresh videos.
    """

    def __init__(self, search_results=None, detail_overrides=None,
                 fail_quota_after=None):
        self.search_results = search_results or {}
        self.detail_overrides = detail_overrides or {}
        self.fail_quota_after = fail_quota_after
        self.search_calls = 0
        self.video_calls = 0
        self.queries: list[dict] = []
        self.detail_ids: list[list[str]] = []

    def has_key(self) -> bool:
        return True

    @property
    def api_key(self):
        return "FAKE_KEY_SHOULD_NOT_LEAK"

    def search(self, q, *, published_after=None, published_before=None,
               order="relevance", max_results=50, safe_search="moderate",
               relevance_language="en", video_embeddable=True):
        if self.fail_quota_after is not None and self.search_calls >= self.fail_quota_after:
            raise yt.QuotaExceeded()
        self.search_calls += 1
        self.queries.append({
            "q": q, "published_after": published_after,
            "published_before": published_before, "order": order,
            "max_results": max_results, "safe_search": safe_search,
            "relevance_language": relevance_language,
            "video_embeddable": video_embeddable,
        })
        chosen = []
        lowered = (q or "").lower()
        for needle, videos in self.search_results.items():
            if needle.lower() in lowered:
                chosen = videos
                break
        if not chosen:
            for videos in self.search_results.values():
                chosen = videos
                break
        return [dict(v) for v in chosen[:max_results]]

    def videos(self, video_ids):
        self.video_calls += 1
        self.detail_ids.append(list(video_ids))
        out = []
        for vid in video_ids:
            if vid in self.detail_overrides:
                out.append(dict(self.detail_overrides[vid]))
                continue
            for videos in self.search_results.values():
                for video in videos:
                    if video["video_id"] == vid:
                        out.append(dict(video))
                        break
        return out


class FakeHTTP:
    """Injected http_get for YouTubeAPI tests; records the outgoing URLs."""

    def __init__(self, handlers=None):
        self.handlers = handlers or {}
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        endpoint = parsed.path.rstrip("/").split("/")[-1]
        handler = self.handlers.get(endpoint)
        if handler is None:
            return 404, json.dumps({"error": {"errors": [
                {"reason": "notFound"}]}}).encode()
        return handler(urllib.parse.parse_qs(parsed.query))
