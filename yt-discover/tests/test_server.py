"""HTTP server tests: security headers, Host check, content type, key safety."""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fake_youtube import FakeYouTube, make_video
from support import TempProject
from yt_discover import server as srv


class ServerTestBase(TempProject):
    def setUp(self):
        super().setUp()
        videos = [
            make_video(f"vid{i:08d}", title=f"video {i}",
                       channel_id=f"UCchan{i:05d}") for i in range(8)
        ]
        self.api = FakeYouTube({"": videos})
        self.httpd = srv.create_server(self.db, self.api, port=0,
                                       log_path=self.state_dir / "server.log")
        self.port = self.httpd.actual_port
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        send_headers = dict(headers or {})
        if host:
            send_headers["Host"] = host
        payload = None
        if body is not None:
            payload = json.dumps(body)
            send_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=send_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data


class HostHeaderTests(ServerTestBase):
    def test_bad_host_rejected(self):
        response, body = self.request("GET", "/api/status",
                                      host="evil.example.com")
        self.assertEqual(response.status, 403)
        self.assertIn("Forbidden host", json.loads(body)["error"])

    def test_localhost_and_127_allowed(self):
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}"):
            response, _ = self.request("GET", "/api/status", host=host)
            self.assertEqual(response.status, 200, host)

    def test_missing_host_rejected(self):
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(b"GET /api/status HTTP/1.0\r\n\r\n")
            data = sock.recv(4096)
        self.assertIn(b"403", data.split(b"\r\n", 1)[0])


class ContentTypeTests(ServerTestBase):
    def test_post_without_json_content_type_rejected(self):
        response, body = self.request(
            "POST", "/api/event", body=None,
            headers={"Content-Type": "text/plain", "Content-Length": "2"})
        self.assertEqual(response.status, 415)
        self.assertIn("Content-Type", json.loads(body)["error"])

    def test_post_with_json_accepted(self):
        response, body = self.request(
            "POST", "/api/topics",
            body={"action": "add", "text": "brand new topic", "lane": "core"})
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(body)["ok"])


class SecurityHeaderTests(ServerTestBase):
    def test_csp_and_no_cors(self):
        response, _ = self.request("GET", "/")
        csp = response.getheader("Content-Security-Policy") or ""
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-src https://www.youtube-nocookie.com", csp)
        self.assertIn("img-src https://i.ytimg.com data:", csp)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_static_index_served(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"yt-discover", body)

    def test_csp_names_nocookie_embed_frames(self):
        response, _ = self.request("GET", "/")
        self.assertIn("https://www.youtube-nocookie.com",
                      response.getheader("Content-Security-Policy") or "")

    def test_app_js_uses_nocookie_embed(self):
        response, body = self.request("GET", "/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn(b"www.youtube-nocookie.com/embed/", body)

    def test_unknown_route_404(self):
        response, _ = self.request("GET", "/api/nope")
        self.assertEqual(response.status, 404)

    def test_path_traversal_blocked(self):
        response, _ = self.request("GET", "/../../etc/passwd")
        self.assertIn(response.status, (403, 404))


class KeySafetyTests(ServerTestBase):
    SECRET = "AIzaFAKE_SECRET_KEY_123456789"

    def test_key_never_in_responses_or_log(self):
        from yt_discover.youtube import YouTubeAPI, QuotaExceeded, YouTubeError

        def boom(*args, **kwargs):
            raise YouTubeError(f"failed calling " + self.SECRET
                               + " https://www.googleapis.com/x?key="
                               + self.SECRET, kind="http")

        api = YouTubeAPI(self.SECRET)
        api.search = boom
        from yt_discover.discovery import DiscoveryEngine
        self.httpd.state.engine = DiscoveryEngine(self.db, api)

        response, body = self.request("POST", "/api/feed", body={"count": 4})
        text = body.decode()
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn("googleapis.com", text)

        log_file = self.state_dir / "server.log"
        if log_file.exists():
            self.assertNotIn(self.SECRET, log_file.read_text())

        db_bytes = (self.data_dir / "discover.db").read_bytes()
        self.assertNotIn(self.SECRET.encode(), db_bytes)
        for suffix in ("-wal", "-shm"):
            extra = self.data_dir / f"discover.db{suffix}"
            if extra.exists():
                self.assertNotIn(self.SECRET.encode(), extra.read_bytes())


class EndpointSmokeTests(ServerTestBase):
    def test_every_endpoint(self):
        response, body = self.request("GET", "/api/status")
        self.assertEqual(response.status, 200)
        data = json.loads(body)
        for key in ("topics", "quota", "settings", "has_key", "words"):
            self.assertIn(key, data)

        response, body = self.request("POST", "/api/feed", body={"count": 3})
        self.assertEqual(response.status, 200)
        feed = json.loads(body)
        self.assertEqual(len(feed["videos"]), 3)
        video = feed["videos"][0]

        response, _ = self.request("POST", "/api/event",
                                   body={"kind": "play",
                                         "video_id": video["video_id"],
                                         "topic": video["topic"]})
        self.assertEqual(response.status, 200)

        response, body = self.request("POST", "/api/topics",
                                      body={"action": "add",
                                            "text": "smoke topic",
                                            "lane": "neighbour"})
        self.assertEqual(response.status, 200)
        topic_id = json.loads(body)["id"]
        self.request("POST", "/api/topics",
                     body={"action": "toggle", "id": topic_id,
                           "enabled": False})
        self.request("POST", "/api/topics",
                     body={"action": "remove", "id": topic_id})

        self.request("POST", "/api/modifiers",
                     body={"action": "add", "text": "smoke modifier"})
        self.request("POST", "/api/words",
                     body={"action": "add", "word": "smokeword"})
        self.request("POST", "/api/words",
                     body={"action": "unblock", "word": "smokeword"})
        self.request("POST", "/api/words",
                     body={"action": "remove", "word": "smokeword"})

        self.request("POST", "/api/styles",
                     body={"action": "add", "kind": "regex",
                           "pattern": "^smoke .*$"})
        self.request("POST", "/api/channels",
                     body={"action": "block", "channel_id": "UCsmoke000001",
                           "title": "Smoke"})
        self.request("POST", "/api/channels",
                     body={"action": "unblock",
                           "channel_id": "UCsmoke000001"})

        self.request("POST", "/api/settings",
                     body={"settings": {"half_life_days": 10}})
        response, body = self.request("POST", "/api/learn", body={})
        self.assertEqual(response.status, 200)
        self.assertIn("blocked_words", json.loads(body))

        response, body = self.request("GET", "/api/export")
        self.assertEqual(response.status, 200)
        export = json.loads(body)
        self.assertIn("tables", export)

        response, _ = self.request("POST", "/api/import",
                                   body={"data": export})
        self.assertEqual(response.status, 200)

        response, body = self.request("POST", "/api/feed",
                                      body={"count": 2, "mode": "wildcard"})
        self.assertEqual(response.status, 200)

        self.request("POST", "/api/reset", body={})
        response, body = self.request("GET", "/api/status")
        self.assertEqual(json.loads(body)["counts"]["seen"], 0)

    def test_invalid_video_id_rejected(self):
        response, body = self.request(
            "POST", "/api/event", body={"kind": "play",
                                        "video_id": "../../etc/passwd"})
        self.assertEqual(response.status, 400)
        self.assertIn("invalid video id", json.loads(body)["error"])

    def test_invalid_event_kind_rejected(self):
        response, _ = self.request(
            "POST", "/api/event", body={"kind": "hack",
                                        "video_id": "dQw4w9WgXcQ"})
        self.assertEqual(response.status, 400)


class StaticSafetyTests(unittest.TestCase):
    def test_app_js_has_no_innerhtml(self):
        text = (ROOT / "yt_discover" / "static" / "app.js").read_text()
        self.assertNotIn("innerHTML", text)
        self.assertNotIn("outerHTML", text)
        self.assertNotIn("insertAdjacentHTML", text)
        self.assertNotIn("document.write", text)

    def test_no_eval_or_new_function(self):
        text = (ROOT / "yt_discover" / "static" / "app.js").read_text()
        self.assertNotIn("eval(", text)
        self.assertNotIn("new Function", text)


if __name__ == "__main__":
    unittest.main()
