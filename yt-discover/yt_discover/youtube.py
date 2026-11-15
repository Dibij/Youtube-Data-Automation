"""YouTube Data API v3 client (official endpoints only) and quota accounting.

Verified against Google's documentation on 2026-11-14:
  * search.list: part, q, type=video, videoEmbeddable, publishedAfter,
    publishedBefore, order (date|rating|relevance|title|videoCount|viewCount),
    relevanceLanguage, safeSearch (none|moderate|strict), maxResults (0-50).
    relatedToVideoId was deprecated 2023-06-12 and removed 2023-08-07.
  * videos.list: part=snippet|contentDetails|statistics, id up to 50 ids.
    snippet.tags, snippet.categoryId, contentDetails.duration (ISO-8601),
    statistics.viewCount are documented fields.
  * Quota: default allocation is 100 search.list calls/day and 10,000 units/day
    for other endpoints, resetting at midnight Pacific.  The old "search costs
    100 units" model is retained here as a configurable dual rail because
    Google's current page describes a separate 100-call search bucket.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - only on exotic builds
    PACIFIC = timezone(timedelta(hours=-8))

API_ROOT = "https://www.googleapis.com/youtube/v3/"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)


def valid_video_id(value: str) -> bool:
    return bool(value) and bool(VIDEO_ID_RE.match(value))


def valid_channel_id(value: str) -> bool:
    return bool(value) and bool(CHANNEL_ID_RE.match(value))


def pacific_day(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(PACIFIC).strftime("%Y-%m-%d")


def parse_duration(value: str | None) -> int | None:
    """ISO-8601 duration (PT1H2M3S, P1DT2H) -> seconds. None when unknown."""
    if not value:
        return None
    match = ISO_DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return (parts["days"] * 86400 + parts["hours"] * 3600
            + parts["minutes"] * 60 + parts["seconds"])


def parse_published(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class YouTubeError(Exception):
    """Any handled API failure. Messages never contain the key or a URL."""

    def __init__(self, message: str, kind: str = "error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class QuotaExceeded(YouTubeError):
    def __init__(self, message: str = "YouTube API daily quota exhausted"):
        super().__init__(message, kind="quota", retryable=False)


class YouTubeAPI:
    """Small stdlib-only wrapper. Inject `http_get` for tests."""

    def __init__(self, api_key: str | None, http_get=None, timeout: float = 20.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._http_get = http_get or self._default_get

    def has_key(self) -> bool:
        return bool(self.api_key)

    def _default_get(self, url: str) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise YouTubeError(f"network error: {type(exc).__name__}",
                               kind="network", retryable=True) from None

    def _call(self, endpoint: str, params: dict) -> dict:
        if not self.api_key:
            raise YouTubeError(
                "No YouTube API key configured. Put YOUTUBE_API_KEY in "
                "~/.config/yt-discover/env or the environment.",
                kind="missing_key",
            )
        query = dict(params)
        query["key"] = self.api_key
        url = API_ROOT + endpoint + "?" + urllib.parse.urlencode(query)
        try:
            status, body = self._http_get(url)
        except YouTubeError:
            raise
        except Exception as exc:  # injected clients may raise anything
            raise YouTubeError(f"request failed: {type(exc).__name__}",
                               kind="network", retryable=True) from None
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            if status >= 400:
                raise YouTubeError(f"YouTube API returned HTTP {status}",
                                   kind="http", retryable=status >= 500) from None
            raise YouTubeError("YouTube API returned malformed JSON",
                               kind="parse") from None
        if status >= 400 or "error" in payload:
            raise self._error_from(payload, status)
        return payload

    def _error_from(self, payload: dict, status: int) -> YouTubeError:
        error = payload.get("error") or {}
        reason = ""
        for item in error.get("errors") or []:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return QuotaExceeded()
        if reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            return YouTubeError("YouTube API rate limit hit", kind="rate",
                                retryable=True)
        if reason in ("keyInvalid", "badRequest") and status in (400, 403):
            return YouTubeError(
                "YouTube API rejected the request (check the API key and that "
                "YouTube Data API v3 is enabled)", kind="auth")
        if reason == "ipRefererBlocked":
            return YouTubeError("API key is restricted in a way that blocks this "
                                "app", kind="auth")
        return YouTubeError(f"YouTube API error (HTTP {status}, reason "
                            f"{reason or 'unknown'})", kind="http",
                            retryable=status >= 500)

    # ------------------------------------------------------------- endpoints
    def search(self, q: str, *, published_after: str | None = None,
               published_before: str | None = None, order: str = "relevance",
               max_results: int = 50, safe_search: str = "moderate",
               relevance_language: str = "en",
               video_embeddable: bool = True) -> list[dict]:
        params = {
            "part": "snippet",
            "q": q,
            "type": "video",
            "maxResults": str(max(0, min(50, int(max_results)))),
            "order": order,
            "safeSearch": safe_search,
            "relevanceLanguage": relevance_language,
        }
        if video_embeddable:
            params["videoEmbeddable"] = "true"
        if published_after:
            params["publishedAfter"] = published_after
        if published_before:
            params["publishedBefore"] = published_before
        payload = self._call("search", params)
        items = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            vid = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not valid_video_id(vid or ""):
                continue
            channel_id = snippet.get("channelId") or ""
            if not valid_channel_id(channel_id):
                continue
            thumbs = snippet.get("thumbnails") or {}
            thumb = (thumbs.get("medium") or thumbs.get("default") or {})
            items.append({
                "video_id": vid,
                "title": str(snippet.get("title") or ""),
                "channel_id": channel_id,
                "channel_title": str(snippet.get("channelTitle") or ""),
                "published_at": str(snippet.get("publishedAt") or ""),
                "published_ts": parse_published(snippet.get("publishedAt")),
                "live_broadcast": str(snippet.get("liveBroadcastContent") or "none"),
                "thumbnail": str(thumb.get("url") or ""),
            })
        return items

    def videos(self, video_ids: list[str]) -> list[dict]:
        ids = [vid for vid in video_ids if valid_video_id(vid)][:50]
        if not ids:
            return []
        params = {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(ids),
            "fields": ("items(id,snippet(title,channelId,channelTitle,publishedAt,"
                       "tags,categoryId,thumbnails/medium/url,liveBroadcastContent)"
                       ",contentDetails/duration,statistics/viewCount)"),
            "maxResults": str(len(ids)),
        }
        payload = self._call("videos", params)
        out = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            vid = item.get("id")
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            stats = item.get("statistics") or {}
            if not valid_video_id(vid or ""):
                continue
            channel_id = snippet.get("channelId") or ""
            if not valid_channel_id(channel_id):
                continue
            tags = snippet.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            views_raw = stats.get("viewCount")
            try:
                views = int(views_raw) if views_raw is not None else None
            except (TypeError, ValueError):
                views = None
            thumbs = snippet.get("thumbnails") or {}
            thumb = thumbs.get("medium") or {}
            out.append({
                "video_id": vid,
                "title": str(snippet.get("title") or ""),
                "channel_id": channel_id,
                "channel_title": str(snippet.get("channelTitle") or ""),
                "published_at": str(snippet.get("publishedAt") or ""),
                "published_ts": parse_published(snippet.get("publishedAt")),
                "duration_seconds": parse_duration(details.get("duration")),
                "views": views,
                "tags": [str(tag) for tag in tags][:40],
                "category_id": str(snippet.get("categoryId") or ""),
                "live_broadcast": str(snippet.get("liveBroadcastContent") or "none"),
                "thumbnail": str(thumb.get("url") or ""),
            })
        return out
