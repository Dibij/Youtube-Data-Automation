"""Day-part (time-of-day profile) tests: windows, intents, lanes, API."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fake_youtube import FakeYouTube, make_video
from support import FakeClock, TempProject
from yt_discover import learning, server as srv

FUN_TOPIC = "naruto fanfiction"
LEARN_TOPIC = "web security explained"


class DaypartWindowTests(unittest.TestCase):
    def profile(self, start, end, name="p"):
        return {"name": name, "start_hour": start, "end_hour": end,
                "enabled": True, "intent_weights": {}}

    def test_simple_window_is_half_open(self):
        profile = self.profile(19, 23)
        self.assertIsNone(learning.active_daypart([profile], 18))
        self.assertIs(learning.active_daypart([profile], 19), profile)
        self.assertIs(learning.active_daypart([profile], 22), profile)
        self.assertIsNone(learning.active_daypart([profile], 23))

    def test_window_wrapping_midnight(self):
        profile = self.profile(23, 6)
        self.assertIs(learning.active_daypart([profile], 23), profile)
        self.assertIs(learning.active_daypart([profile], 0), profile)
        self.assertIs(learning.active_daypart([profile], 5), profile)
        self.assertIsNone(learning.active_daypart([profile], 6))

    def test_disabled_profile_never_matches(self):
        profile = {**self.profile(0, 24), "enabled": False}
        self.assertIsNone(learning.active_daypart([profile], 12))

    def test_intent_visibility(self):
        settings = {"daypart_min_intent_weight": 0.05}
        evening = {"intent_weights": {"fun": 1.0, "learning": 0.0}}
        self.assertTrue(learning.daypart_allows_intent(evening, "fun", settings))
        self.assertFalse(
            learning.daypart_allows_intent(evening, "learning", settings))
        self.assertTrue(learning.daypart_allows_intent(None, "learning", settings))
        self.assertTrue(learning.daypart_allows_intent(
            {"intent_weights": {}}, "anything", settings))

    def test_missing_intent_defaults_to_hidden(self):
        settings = {"daypart_min_intent_weight": 0.05}
        evening = {"intent_weights": {"fun": 1.0}}
        self.assertFalse(
            learning.daypart_allows_intent(evening, "learning", settings))

    def test_lane_share_override(self):
        base = {"lane_core_share": 0.4, "lane_neighbour_share": 0.4,
                "lane_wildcard_share": 0.2, "explore": 0.0}
        evening = {"lane_core": 0.2, "lane_neighbour": 0.3, "lane_wildcard": 0.5}
        shares = learning.effective_lane_shares(base, evening)
        self.assertAlmostEqual(shares["core"], 0.2)
        self.assertAlmostEqual(shares["wildcard"], 0.5)


class DaypartSelectionTests(TempProject):
    def setUp(self):
        super().setUp()
        self.db.add_topic(FUN_TOPIC, "core", intent="fun")
        self.db.add_topic(LEARN_TOPIC, "core", intent="learning")
        self.db.add_topic("game design analysis", "neighbour", intent="learning")
        self.db.add_topic("restoration", "wildcard", intent="fun")
        self.api = FakeYouTube({"": [
            make_video(f"dpv{i:08d}", title=f"video {i}",
                       channel_id=f"UCdp{i:05d}") for i in range(40)
        ]})

    def engine_at_hour(self, hour):
        moment = datetime.now().replace(hour=hour, minute=0, second=0,
                                        microsecond=0).timestamp()
        clock = FakeClock(moment)
        self.db.clock = clock
        engine = self.engine(self.api)
        engine.clock = clock
        return engine, clock

    def searched_topics(self, engine, batches=12):
        topics = []
        for _ in range(batches):
            payload = engine.fetch_batch(count=1)
            topics.extend(q["q"] for q in self.api.queries[len(topics):])
            self.db.clear_history()
        return topics

    def test_evening_prefers_fun_and_day_prefers_learning(self):
        evening, _ = self.engine_at_hour(21)
        evening_topics = self.searched_topics(evening, batches=20)
        self.assertTrue(any(FUN_TOPIC in q for q in evening_topics))
        self.assertFalse(any(LEARN_TOPIC in q for q in evening_topics),
                         evening_topics)

        self.api.queries.clear()
        self.api.search_calls = 0
        day, _ = self.engine_at_hour(10)
        day_topics = self.searched_topics(day, batches=20)
        self.assertTrue(any(LEARN_TOPIC in q for q in day_topics))
        self.assertFalse(any(FUN_TOPIC in q for q in day_topics), day_topics)

    def test_seven_pm_boundary_switches_to_fun(self):
        engine, _ = self.engine_at_hour(18)
        self.assertEqual(engine.current_daypart(self.db.get_settings(),
                                                engine.now())["name"],
                         "Learning day")
        engine, _ = self.engine_at_hour(19)
        profile = engine.current_daypart(self.db.get_settings(), engine.now())
        self.assertEqual(profile["name"], "Evening fun")

    def test_empty_daypart_falls_back_instead_of_empty_feed(self):
        for profile in self.db.list_dayparts():
            self.db.update_daypart(profile["id"],
                                   intent_weights={"nothing_matches": 1.0})
        engine, _ = self.engine_at_hour(21)
        payload = engine.fetch_batch(count=3)
        self.assertTrue(payload["videos"],
                        "an unusable day part must not produce an empty feed")
        self.assertTrue(any("day part" in m.lower()
                            for m in payload["messages"]), payload["messages"])

    def test_disabled_dayparts_use_base_shares(self):
        self.db.set_setting("dayparts_enabled", False)
        for profile in self.db.list_dayparts():
            self.db.update_daypart(profile["id"],
                                   intent_weights={"learning": 0.0, "fun": 0.0})
        engine, _ = self.engine_at_hour(21)
        self.assertIsNone(engine.current_daypart(self.db.get_settings(),
                                                 engine.now()))
        self.assertTrue(engine.fetch_batch(count=3)["videos"])

    def test_intent_weights_shape_thompson_evidence(self):
        import random
        rng = random.Random(5)
        candidates = [
            {"id": 1, "text": "a", "intent": "fun"},
            {"id": 2, "text": "b", "intent": "learning"},
        ]
        scores = {"a": {"pos": 0.0, "neg": 0.0},
                  "b": {"pos": 25.0, "neg": 0.0}}
        picks = [learning.thompson_pick(
            candidates, scores, rng, epsilon=0.0,
            intent_weights={"fun": 1.0, "learning": 0.0})["text"]
            for _ in range(50)]
        self.assertEqual(set(picks), {"a"},
                         "an intent weighted 0 must never be chosen")


class DaypartPersistenceTests(TempProject):
    def test_defaults_exist_and_survive_restart(self):
        profiles = self.db.list_dayparts()
        self.assertGreaterEqual(len(profiles), 3)
        names = {p["name"] for p in profiles}
        self.assertIn("Learning day", names)
        self.assertIn("Evening fun", names)

        evening = next(p for p in profiles if p["name"] == "Evening fun")
        self.db.update_daypart(evening["id"], start_hour=20,
                               intent_weights={"fun": 1, "learning": 0})
        path = self.db.path
        self.db.close()
        from yt_discover.db import Database
        reopened = Database(path, clock=self.clock)
        again = next(p for p in reopened.list_dayparts()
                     if p["name"] == "Evening fun")
        self.assertEqual(again["start_hour"], 20)
        self.assertEqual(again["intent_weights"]["fun"], 1)
        self.db = reopened

    def test_topic_intent_persists_and_seed_can_set_it(self):
        topic = next(t for t in self.db.list_topics() if t["text"] == "core alpha")
        self.db.set_topic_intent(topic["id"], "fun")
        self.assertEqual(
            next(t for t in self.db.list_topics()
                 if t["id"] == topic["id"])["intent"], "fun")

        self.seed_file.write_text(json.dumps({
            "topics": {"core": [{"text": "seed fun topic", "intent": "fun"}]},
        }), encoding="utf-8")
        self.db.reload_seed(self.seed_file)
        added = next(t for t in self.db.list_topics()
                     if t["text"] == "seed fun topic")
        self.assertEqual(added["intent"], "fun")

    def test_reload_seed_is_additive_and_idempotent(self):
        shelf = "shelf topic"
        first = self.db.reload_seed(self.seed_file)
        self.assertTrue(first["ok"])
        second = self.db.reload_seed(self.seed_file)
        self.assertTrue(second["ok"])
        self.assertEqual(second["added"], {})
        topics = [t["text"] for t in self.db.list_topics()]
        self.assertEqual(topics.count(shelf), 0)

    def test_export_import_roundtrips_dayparts(self):
        evening = next(p for p in self.db.list_dayparts()
                       if p["name"] == "Evening fun")
        self.db.update_daypart(evening["id"], end_hour=22)
        snapshot = self.db.export_all()
        self.db.clear_history()
        self.db.update_daypart(evening["id"], end_hour=23)
        self.db.import_all(snapshot)
        again = next(p for p in self.db.list_dayparts()
                     if p["name"] == "Evening fun")
        self.assertEqual(again["end_hour"], 22)

    def test_legacy_database_gains_intent_column(self):
        import sqlite3
        legacy = self.tmp / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.executescript("""
            CREATE TABLE topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                lane TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'user',
                protected INTEGER NOT NULL DEFAULT 0,
                searches INTEGER NOT NULL DEFAULT 0,
                plays INTEGER NOT NULL DEFAULT 0,
                rejects INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE (text, lane)
            );
            INSERT INTO topics(text,lane,created_at) VALUES('old topic','core',1);
        """)
        conn.commit()
        conn.close()
        from yt_discover.db import Database
        migrated = Database(legacy, clock=self.clock)
        topic = next(t for t in migrated.list_topics()
                     if t["text"] == "old topic")
        self.assertEqual(topic["intent"], "learning")
        migrated.close()


class DaypartApiTests(TempProject):
    def setUp(self):
        super().setUp()
        self.api = FakeYouTube({"": [
            make_video(f"apiv{i:07d}", channel_id=f"UCapi{i:05d}") for i in range(9)
        ]})
        self.httpd = srv.create_server(self.db, self.api, port=0)
        self.port = self.httpd.actual_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body)
        conn.request("POST", path, body=payload,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_status_exposes_dayparts_and_intents(self):
        status, data = self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("dayparts", data)
        self.assertIn("active_daypart", data)
        self.assertIn("lane_shares_now", data)
        self.assertIn("local_hour", data)
        self.assertIn("learning", data["intents"])

    def test_update_add_toggle_remove_daypart(self):
        status, data = self.get("/api/status")
        target = data["dayparts"][0]
        status, _ = self.post("/api/dayparts", {"action": "update",
                                                "id": target["id"],
                                                "fields": {"start_hour": 7}})
        self.assertEqual(status, 200)
        status, data = self.get("/api/status")
        self.assertEqual(next(p for p in data["dayparts"]
                              if p["id"] == target["id"])["start_hour"], 7)

        status, data = self.post("/api/dayparts", {"action": "add", "daypart": {
            "name": "Nap time", "start_hour": 13, "end_hour": 14,
            "intent_weights": {"fun": 1}}})
        self.assertEqual(status, 200)
        new_id = data["id"]

        status, _ = self.post("/api/dayparts", {"action": "toggle",
                                                "id": new_id, "enabled": False})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/dayparts", {"action": "remove",
                                                "id": new_id})
        self.assertEqual(status, 200)

    def test_topic_add_with_intent(self):
        status, data = self.post("/api/topics", {"action": "add",
                                                 "text": "naruto fanfiction 2",
                                                 "lane": "core",
                                                 "intent": "fun"})
        self.assertEqual(status, 200)
        status, snapshot = self.get("/api/status")
        topic = next(t for t in snapshot["topics"]
                     if t["text"] == "naruto fanfiction 2")
        self.assertEqual(topic["intent"], "fun")

    def test_seed_reload_endpoint(self):
        status, data = self.post("/api/seed", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
