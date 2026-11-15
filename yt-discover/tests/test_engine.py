"""Tests for the discovery engine: filters, pool, quota, learning."""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fake_youtube import FakeYouTube, make_video
from support import TempProject
from yt_discover import learning, youtube as yt


def vid(n: int) -> str:
    base = f"vid{n:08d}"
    return base[:11]


class FilterTests(TempProject):
    def _api(self, videos):
        return FakeYouTube({"": videos})

    def test_seen_exclusion_across_batches(self):
        videos = [make_video(vid(i), title=f"video {i}",
                             channel_id=f"UCchan{i:05d}") for i in range(10)]
        api = self._api(videos)
        engine = self.engine(api)
        first = engine.fetch_batch(count=5)
        second = engine.fetch_batch(count=5)
        ids_first = {v["video_id"] for v in first["videos"]}
        ids_second = {v["video_id"] for v in second["videos"]}
        self.assertTrue(ids_first)
        self.assertFalse(ids_first & ids_second)
        self.assertTrue(all(self.db.is_seen(v) for v in ids_first | ids_second))

    def test_per_channel_cap(self):
        videos = [make_video(vid(i), channel_id="UCsame00001") for i in range(12)]
        api = self._api(videos)
        engine = self.engine(api)
        batch = engine.fetch_batch(count=12)
        self.assertLessEqual(len(batch["videos"]), 2)
        channels = [v["channel_id"] for v in batch["videos"]]
        self.assertEqual(channels.count("UCsame00001"), len(channels))

    def test_short_and_live_filtered(self):
        videos = [
            make_video(vid(1), duration=30, channel_id="UCchan00001"),
            make_video(vid(2), duration=61, channel_id="UCchan00002"),
            make_video(vid(3), live="live", channel_id="UCchan00003"),
            make_video(vid(4), live="upcoming", channel_id="UCchan00004"),
            make_video(vid(5), duration=300, channel_id="UCchan00005"),
        ]
        api = self._api(videos)
        engine = self.engine(api)
        batch = engine.fetch_batch(count=10)
        ids = {v["video_id"] for v in batch["videos"]}
        self.assertNotIn(vid(1), ids)
        self.assertIn(vid(2), ids)
        self.assertNotIn(vid(3), ids)
        self.assertNotIn(vid(4), ids)
        self.assertIn(vid(5), ids)

    def test_blocked_channel_and_word(self):
        videos = [
            make_video(vid(1), title="fine video", channel_id="UCgood00001"),
            make_video(vid(2), title="fine other", channel_id="UCbad000001"),
            make_video(vid(3), title="the summoner returns", channel_id="UCgood00002"),
        ]
        self.db.block_channel("UCbad000001", "Bad")
        api = self._api(videos)
        engine = self.engine(api)
        batch = engine.fetch_batch(count=10)
        ids = {v["video_id"] for v in batch["videos"]}
        self.assertIn(vid(1), ids)
        self.assertNotIn(vid(2), ids)
        self.assertNotIn(vid(3), ids)

    def test_duration_filters(self):
        videos = [
            make_video(vid(1), duration=300, channel_id="UCchan00001"),
            make_video(vid(2), duration=1500, channel_id="UCchan00002"),
            make_video(vid(3), duration=2400, channel_id="UCchan00003"),
        ]
        api = self._api(videos)
        engine = self.engine(api)
        self.db.set_setting("duration_filter", "short")
        short = engine.fetch_batch(count=10)
        ids = {v["video_id"] for v in short["videos"]}
        self.assertIn(vid(1), ids)
        self.assertNotIn(vid(2), ids)
        self.assertNotIn(vid(3), ids)

        self.db.clear_history()
        self.db.set_setting("duration_filter", "long")
        long_batch = engine.fetch_batch(count=10)
        ids = {v["video_id"] for v in long_batch["videos"]}
        self.assertNotIn(vid(1), ids)
        self.assertIn(vid(2), ids)
        self.assertIn(vid(3), ids)

    def test_hidden_gems(self):
        videos = [
            make_video(vid(1), views=50, channel_id="UCchan00001"),
            make_video(vid(2), views=50000, channel_id="UCchan00002"),
            make_video(vid(3), views=5_000_000, channel_id="UCchan00003"),
        ]
        api = self._api(videos)
        engine = self.engine(api)
        self.db.set_setting("hidden_gems", True)
        batch = engine.fetch_batch(count=10)
        ids = {v["video_id"] for v in batch["videos"]}
        self.assertNotIn(vid(1), ids)
        self.assertIn(vid(2), ids)
        self.assertNotIn(vid(3), ids)

    def test_unknown_duration_from_search_hint(self):
        videos = [make_video(vid(1), duration=None)]
        api = FakeYouTube({"": videos})
        engine = self.engine(api)
        batch = engine.fetch_batch(count=5)
        self.assertEqual(batch["videos"], [])


class PoolAndQuotaTests(TempProject):
    def test_pool_serves_without_search_and_expires(self):
        videos = [make_video(vid(i), channel_id=f"UCchan{i:05d}") for i in range(6)]
        api = FakeYouTube({"": videos})
        engine = self.engine(api)
        self.db.set_setting("batch_size", 2)
        self.db.set_setting("max_searches_per_batch", 1)
        first = engine.fetch_batch(count=2)
        self.assertEqual(len(first["videos"]), 2)
        calls_after_first = api.search_calls
        self.assertEqual(self.db.pool_count(), 4)

        second = engine.fetch_batch(count=2)
        self.assertEqual(len(second["videos"]), 2)
        self.assertEqual(api.search_calls, calls_after_first)
        self.assertEqual(self.db.pool_count(), 2)

        self.clock.days(4)
        third = engine.fetch_batch(count=2)
        self.assertGreaterEqual(api.search_calls, calls_after_first)
        self.assertEqual(self.db.pool_count(), 0)
        self.assertTrue(third["videos"])

    def test_quota_accounting(self):
        videos = [make_video(vid(i), channel_id=f"UCchan{i:05d}") for i in range(5)]
        api = FakeYouTube({"": videos})
        engine = self.engine(api)
        self.db.set_setting("batch_size", 5)
        engine.fetch_batch(count=5)
        status = engine.quota_status()
        self.assertEqual(status["search_calls"], api.search_calls)
        self.assertEqual(status["units"], 100 * api.search_calls + api.video_calls)
        self.assertEqual(status["videos_calls"], api.video_calls)

    def test_quota_exhaustion_is_graceful(self):
        videos = [make_video(vid(i), channel_id=f"UCchan{i:05d}") for i in range(10)]
        api = FakeYouTube({"": videos}, fail_quota_after=1)
        engine = self.engine(api)
        self.db.set_setting("batch_size", 20)
        self.db.set_setting("max_searches_per_batch", 4)
        first = engine.fetch_batch(count=20)
        self.assertTrue(first["videos"])
        self.assertTrue(any("quota" in m.lower() for m in first["messages"]))
        self.assertLessEqual(api.search_calls, 2)


class DecayAndBanditTests(TempProject):
    def test_old_events_count_less(self):
        now = self.clock.now
        events = [
            {"ts": now, "kind": "play", "weight": 1.0, "topic_text": "t"},
            {"ts": now - 14 * 86400, "kind": "play", "weight": 1.0,
             "topic_text": "t"},
        ]
        total = learning.decayed_sum(events, now, 14)
        self.assertAlmostEqual(total, 1.5, places=3)

    def test_thompson_favours_liked_but_explores(self):
        rng = random.Random(7)
        topics = [{"id": 1, "text": "liked"}, {"id": 2, "text": "disliked"}]
        scores = {"liked": {"pos": 20.0, "neg": 0.0, "score": 20.0},
                  "disliked": {"pos": 0.0, "neg": 8.0, "score": -8.0}}
        picks = [learning.thompson_pick(topics, scores, rng, epsilon=0.1)["text"]
                 for _ in range(400)]
        liked = picks.count("liked")
        self.assertGreater(liked, picks.count("disliked"))
        self.assertGreater(picks.count("disliked"), 0)

    def test_lane_floor_and_topic_ceiling(self):
        settings = {
            "lane_core_share": 0.95,
            "lane_neighbour_share": 0.03,
            "lane_wildcard_share": 0.02,
            "explore": 0.0,
            "exploration_floor": 0.25,
            "exploration_window": 20,
            "topic_ceiling": 0.3,
            "batch_size": 20,
            "max_searches_per_batch": 5,
        }
        rng = random.Random(3)
        planner = learning.LanePlanner(settings, batch_size=20)
        lanes = [planner.choose_lane(rng) for _ in range(200)]
        explore_share = sum(1 for l in lanes if l != "core") / len(lanes)
        self.assertGreaterEqual(explore_share, 0.20)
        self.assertLessEqual(planner.topic_allowance(), 2)

    def test_lane_shares_respect_floor_over_windows(self):
        settings = {
            "lane_core_share": 0.4, "lane_neighbour_share": 0.4,
            "lane_wildcard_share": 0.2, "explore": 0.0,
            "exploration_floor": 0.25, "exploration_window": 10,
            "topic_ceiling": 0.3, "batch_size": 12,
            "max_searches_per_batch": 4,
        }
        rng = random.Random(11)
        planner = learning.LanePlanner(settings, batch_size=12)
        lanes = [planner.choose_lane(rng) for _ in range(200)]
        for start in range(0, 200 - 10, 10):
            window = lanes[start:start + 10]
            non_core = sum(1 for l in window if l != "core") / len(window)
            self.assertGreaterEqual(non_core, 0.2,
                                    f"window {start} fell below floor: {window}")

    def test_effective_shares_with_explore(self):
        shares = learning.effective_lane_shares({
            "lane_core_share": 0.4, "lane_neighbour_share": 0.4,
            "lane_wildcard_share": 0.2, "explore": 1.0})
        self.assertAlmostEqual(shares["core"], 0.0, places=6)
        self.assertAlmostEqual(shares["neighbour"] + shares["wildcard"], 1.0)


class WordLearningTests(TempProject):
    def _make_events(self, videos, weights):
        now = self.clock.now
        video_map = {}
        for index, (video, weight) in enumerate(zip(videos, weights)):
            self.db.upsert_video({**video, "first_seen": now})
            video_map[video["video_id"]] = video
            kind = "not_this" if weight < 0 else "play"
            self.db.add_event(video["video_id"], "core alpha", video["channel_id"],
                              kind, weight, now - 3600)
        return video_map

    def test_word_auto_blocked_only_when_all_conditions_hold(self):
        videos = [make_video(vid(i), title="rebar tips worth knowing",
                             channel_id=f"UCc{i:07d}") for i in range(4)]
        self._make_events(videos, [-1.5, -1.5, -1.5, -0.2])
        added = learning.learn_words(self.db, self.clock.now)
        self.assertIn("rebar", added)
        word = {w["word"]: w for w in self.db.list_avoid_words()}["rebar"]
        self.assertTrue(word["blocked"])

    def test_two_negatives_not_enough(self):
        videos = [make_video(vid(i), title="glorp tips", channel_id=f"UCc{i:07d}")
                  for i in range(2)]
        self._make_events(videos, [-1.5, -1.5])
        added = learning.learn_words(self.db, self.clock.now)
        self.assertNotIn("glorp", added)

    def test_low_negative_share_not_blocked(self):
        videos = [make_video(vid(i), title="quux tricks", channel_id=f"UCc{i:07d}")
                  for i in range(5)]
        self._make_events(videos, [-1.0, -1.0, -1.0, 4.0, 4.0])
        added = learning.learn_words(self.db, self.clock.now)
        self.assertNotIn("quux", added)

    def test_unblocking_protects(self):
        videos = [make_video(vid(i), title="rebar tips", channel_id=f"UCc{i:07d}")
                  for i in range(4)]
        self._make_events(videos, [-1.5, -1.5, -1.5, -1.5])
        learning.learn_words(self.db, self.clock.now)
        self.db.set_avoid_word("rebar", False, protected=True)
        learning.learn_words(self.db, self.clock.now + 60)
        row = {w["word"]: w for w in self.db.list_avoid_words()}["rebar"]
        self.assertFalse(row["blocked"])
        self.assertTrue(row["protected"])

    def test_auto_block_cap(self):
        settings = self.db.get_settings()
        settings["max_auto_blocked_words"] = 1
        videos = [make_video(vid(i), title=f"zzword{i} tips",
                             channel_id=f"UCc{i:07d}") for i in range(4)]
        self._make_events(videos, [-1.5] * 4)
        added = learning.learn_words(self.db, self.clock.now)
        self.assertLessEqual(len(added), 1)


class StyleLearningTests(TempProject):
    def test_style_feature_created_from_caps_titles(self):
        now = self.clock.now
        videos = []
        for i in range(4):
            video = make_video(vid(i), title="THIS IS SHOUTING",
                               channel_id=f"UCc{i:07d}")
            self.db.upsert_video({**video, "first_seen": now})
            self.db.add_event(video["video_id"], "core alpha", video["channel_id"],
                              "not_this", -1.5, now)
            videos.append(video)
        created = learning.learn_style(self.db, now)
        kinds = {item["kind"] for item in created}
        self.assertIn("caps", kinds)
        features = self.db.list_style_features()
        self.assertTrue(any(f["kind"] == "caps" and f["enabled"] for f in features))

    def test_user_regex_feature_blocks_titles(self):
        self.db.add_style_feature("regex", r"^I .+ as .+", "user")
        settings = self.db.get_settings()
        features = learning.active_style_filters(self.db)
        title = "I rebuilt a PC as a toaster"
        self.assertTrue(learning.matches_blocked_style(title, features, settings, set()))
        self.assertFalse(learning.matches_blocked_style("Completely different",
                                                        features, settings, set()))


class TrialTopicTests(TempProject):
    def test_trial_created_capped_and_retired(self):
        now = self.clock.now
        self.db.set_setting("max_trial_topics", 2)
        self.db.set_setting("trial_max_new_per_refresh", 2)
        settings = self.db.get_settings()
        videos = [
            make_video(vid(1), title="minecraft redstone basics",
                       tags=["redstone engineering", "logic circuits"],
                       channel_id="UCchan00001"),
            make_video(vid(2), title="pixel art workflow",
                       tags=["pixel art", "sprite design"],
                       channel_id="UCchan00002"),
        ]
        for video in videos:
            self.db.upsert_video({**video, "first_seen": now})
        for video in videos:
            self.db.add_event(video["video_id"], "core alpha", video["channel_id"],
                              "more_like_this", 3.0, now)
        created = learning.propose_trial_topics(self.db, now)
        self.assertLessEqual(len(created), 2)
        self.assertLessEqual(len(self.db.list_trials("active")), 2)

        trial = self.db.list_trials("active")[0]
        while trial["searches_used"] < trial["budget"]:
            self.db.bump_trial(trial["topic_id"], "searches_used")
            trial = self.db.get_trial_for_topic(trial["topic_id"])
        for _ in range(settings["trial_min_impressions"]):
            self.db.bump_trial(trial["topic_id"], "impressions")
        retired = learning.evaluate_trials(self.db, settings)
        self.assertIn(trial["text"], retired)
        self.assertTrue(learning.revive_trial(self.db, trial["topic_id"]))
        revived = self.db.get_trial_for_topic(trial["topic_id"])
        self.assertEqual(revived["status"], "active")

    def test_trial_not_created_without_likes(self):
        created = learning.propose_trial_topics(self.db, self.clock.now)
        self.assertEqual(created, [])


class PersistenceTests(TempProject):
    def test_state_persists_across_restart(self):
        self.db.add_topic("persisted topic", "core")
        self.db.set_setting("half_life_days", 7)
        path = self.db.path
        self.db.close()
        from yt_discover.db import Database
        reopened = Database(path, clock=self.clock)
        texts = [t["text"] for t in reopened.list_topics()]
        self.assertIn("persisted topic", texts)
        self.assertEqual(reopened.get_setting("half_life_days"), 7)
        self.db = reopened

    def test_export_import_roundtrip(self):
        self.db.add_topic("roundtrip", "wildcard")
        self.db.add_avoid_word("roundtripword")
        self.db.set_setting("batch_size", 9)
        snapshot = self.db.export_all()
        self.db.clear_history()
        self.db.remove_topic([t for t in self.db.list_topics()
                              if t["text"] == "roundtrip"][0]["id"])
        self.db.import_all(snapshot)
        texts = [t["text"] for t in self.db.list_topics()]
        self.assertIn("roundtrip", texts)
        self.assertEqual(self.db.get_setting("batch_size"), 9)
        self.assertIn("roundtripword", [w["word"] for w in self.db.list_avoid_words()])


class YouTubeParsingTests(unittest.TestCase):
    def test_duration_parser(self):
        self.assertEqual(yt.parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(yt.parse_duration("P1DT1S"), 86401)
        self.assertEqual(yt.parse_duration("PT45S"), 45)
        self.assertIsNone(yt.parse_duration("garbage"))

    def test_video_id_validation(self):
        self.assertTrue(yt.valid_video_id("dQw4w9WgXcQ"))
        self.assertFalse(yt.valid_video_id("short"))
        self.assertFalse(yt.valid_video_id("bad id with spaces"))
        self.assertTrue(yt.valid_channel_id("UCabcdefghijklmnopqrstuv"))

    def test_pacific_day_uses_pacific_midnight(self):
        from datetime import datetime, timezone
        moment = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(yt.pacific_day(moment), "2026-11-13")
        moment2 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(yt.pacific_day(moment2), "2026-11-14")


if __name__ == "__main__":
    unittest.main()
