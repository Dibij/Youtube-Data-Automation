"""End-to-end smoke test: real server subprocess + fake YouTube API."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ytd-smoke-"))
        config_dir = cls.tmp / "config"
        data_dir = cls.tmp / "data"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        (config_dir / "seed.json").write_text(json.dumps({
            "topics": {"core": ["minecraft redstone"],
                       "neighbour": ["game design analysis"],
                       "wildcard": ["deep sea documentary"]},
            "query_modifiers": ["explained"],
            "avoid_words": ["reborn"],
        }), encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "YT_DISCOVER_DATA_DIR": str(data_dir),
            "YT_DISCOVER_CONFIG_DIR": str(config_dir),
            "YT_DISCOVER_STATE_DIR": str(cls.tmp / "state"),
            "YT_DISCOVER_SEED": str(config_dir / "seed.json"),
            "PYTHONPATH": str(ROOT),
        })
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "serve_fake.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        line = cls.proc.stdout.readline().strip()
        if not line.isdigit():
            stderr = cls.proc.stderr.read()
            raise RuntimeError(f"server did not start: {line!r} {stderr}")
        cls.port = int(line)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def test_full_flow(self):
        response, body = self.request("GET", "/api/status")
        self.assertEqual(response.status, 200)
        status = json.loads(body)
        self.assertTrue(status["has_key"])
        self.assertEqual(len(status["topics"]), 3)

        response, body = self.request("POST", "/api/feed", body={"count": 6})
        self.assertEqual(response.status, 200)
        feed = json.loads(body)
        self.assertEqual(len(feed["videos"]), 6)
        first = feed["videos"][0]
        self.assertRegex(first["video_id"], r"^[A-Za-z0-9_-]{11}$")

        response, body = self.request("POST", "/api/event", body={
            "kind": "more_like_this", "video_id": first["video_id"],
            "topic": first["topic"]})
        self.assertEqual(response.status, 200)
        changed = json.loads(body)["changed"]
        self.assertIn("blocked_words", changed)

        response, body = self.request("POST", "/api/feed", body={"count": 6})
        second = json.loads(body)
        ids_a = {v["video_id"] for v in feed["videos"]}
        ids_b = {v["video_id"] for v in second["videos"]}
        self.assertFalse(ids_a & ids_b)

        response, body = self.request("POST", "/api/learn", body={})
        self.assertEqual(response.status, 200)

        response, body = self.request("GET", "/api/export")
        export = json.loads(body)
        self.assertIn("tables", export)

        response, _ = self.request("POST", "/api/import", body={"data": export})
        self.assertEqual(response.status, 200)

        response, body = self.request("POST", "/api/reset", body={})
        self.assertEqual(response.status, 200)

    def test_asset_routes(self):
        for path, fragment in (("/", b"yt-discover"),
                               ("/app.js", b"textContent"),
                               ("/style.css", b"--bg")):
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 200, path)
            self.assertIn(fragment, body, path)


if __name__ == "__main__":
    unittest.main()
