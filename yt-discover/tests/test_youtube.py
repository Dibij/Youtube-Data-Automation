"""YouTubeAPI client tests using an injected fake HTTP getter (no network)."""
from __future__ import annotations

import json
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fake_youtube import FakeHTTP  # noqa: E402
from yt_discover import youtube as yt  # noqa: E402


def search_payload(ids):
    return json.dumps({"items": [
        {"id": {"videoId": vid}, "snippet": {
            "title": f"title {vid}", "channelId": "UCabcdefghijklmnopqrstuv",
            "channelTitle": "Chan", "publishedAt": "2026-01-02T03:04:05Z",
            "liveBroadcastContent": "none",
            "thumbnails": {"medium": {"url": f"https://i.ytimg.com/vi/{vid}/x.jpg"}},
        }} for vid in ids]}).encode()


def videos_payload(ids):
    return json.dumps({"items": [
        {"id": vid,
         "snippet": {"title": f"title {vid}",
                     "channelId": "UCabcdefghijklmnopqrstuv",
                     "channelTitle": "Chan",
                     "publishedAt": "2026-01-02T03:04:05Z",
                     "tags": ["redstone", "tutorial"],
                     "categoryId": "27",
                     "liveBroadcastContent": "none",
                     "thumbnails": {"medium": {"url": "https://i.ytimg.com/x.jpg"}}},
         "contentDetails": {"duration": "PT12M34S"},
         "statistics": {"viewCount": "12345"}}
        for vid in ids]}).encode()


class SearchParameterTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP({
            "search": lambda q: (200, search_payload(["dQw4w9WgXcQ"])),
            "videos": lambda q: (200, videos_payload(q.get("id", [""])[0].split(","))),
        })
        self.api = yt.YouTubeAPI("FAKE_KEY", http_get=self.http)

    def _query(self, index=0):
        url = self.http.urls[index]
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    def test_search_parameters_match_documented_names(self):
        self.api.search("minecraft redstone explained",
                        published_after="2025-01-01T00:00:00Z",
                        published_before="2025-06-01T00:00:00Z",
                        order="date", max_results=50, safe_search="strict",
                        relevance_language="en", video_embeddable=True)
        q = self._query()
        self.assertEqual(q["part"], ["snippet"])
        self.assertEqual(q["type"], ["video"])
        self.assertEqual(q["videoEmbeddable"], ["true"])
        self.assertEqual(q["publishedAfter"], ["2025-01-01T00:00:00Z"])
        self.assertEqual(q["publishedBefore"], ["2025-06-01T00:00:00Z"])
        self.assertEqual(q["order"], ["date"])
        self.assertEqual(q["maxResults"], ["50"])
        self.assertEqual(q["safeSearch"], ["strict"])
        self.assertEqual(q["relevanceLanguage"], ["en"])
        self.assertNotIn("relatedToVideoId", q)
        self.assertNotIn("videoDuration", q)

    def test_max_results_is_clamped_to_50(self):
        self.api.search("x", max_results=999)
        self.assertEqual(self._query()["maxResults"], ["50"])

    def test_search_parse(self):
        items = self.api.search("x")
        self.assertEqual(items[0]["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(items[0]["live_broadcast"], "none")
        self.assertGreater(items[0]["published_ts"], 0)

    def test_invalid_video_ids_dropped_from_search(self):
        self.http.handlers["search"] = lambda q: (200, search_payload(["short", "bad id"]) )
        self.assertEqual(self.api.search("x"), [])

    def test_videos_parts_and_fields(self):
        items = self.api.videos(["dQw4w9WgXcQ"])
        q = self._query()
        self.assertIn("snippet", q["part"][0])
        self.assertIn("contentDetails", q["part"][0])
        self.assertIn("statistics", q["part"][0])
        self.assertIn("statistics/viewCount", q["fields"][0])
        self.assertEqual(q["id"], ["dQw4w9WgXcQ"])
        self.assertEqual(items[0]["duration_seconds"], 754)
        self.assertEqual(items[0]["views"], 12345)
        self.assertEqual(items[0]["tags"], ["redstone", "tutorial"])
        self.assertEqual(items[0]["category_id"], "27")

    def test_videos_limited_to_50_ids(self):
        ids = [f"vid{i:08d}" for i in range(60)]
        self.api.videos(ids)
        q = self._query()
        self.assertEqual(len(q["id"][0].split(",")), 50)

    def test_invalid_ids_never_sent(self):
        self.assertEqual(self.api.videos(["../../etc/passwd"]), [])
        self.assertEqual(self.http.urls, [])


class ErrorHandlingTests(unittest.TestCase):
    def test_quota_error_mapped(self):
        body = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}}).encode()
        api = yt.YouTubeAPI("K", http_get=lambda url: (403, body))
        with self.assertRaises(yt.QuotaExceeded):
            api.search("x")

    def test_auth_error_mapped_without_url(self):
        body = json.dumps({"error": {"errors": [{"reason": "keyInvalid"}]}}).encode()
        api = yt.YouTubeAPI("K", http_get=lambda url: (400, body))
        with self.assertRaises(yt.YouTubeError) as ctx:
            api.search("x")
        self.assertNotIn("http", str(ctx.exception))
        self.assertNotIn("K", str(ctx.exception))

    def test_network_error_mapped(self):
        def boom(url):
            raise OSError("connection refused")
        api = yt.YouTubeAPI("K", http_get=boom)
        with self.assertRaises(yt.YouTubeError) as ctx:
            api.search("x")
        self.assertEqual(ctx.exception.kind, "network")

    def test_missing_key_is_clear(self):
        api = yt.YouTubeAPI("")
        with self.assertRaises(yt.YouTubeError) as ctx:
            api.search("x")
        self.assertEqual(ctx.exception.kind, "missing_key")
        self.assertFalse(api.has_key())


if __name__ == "__main__":
    unittest.main()
