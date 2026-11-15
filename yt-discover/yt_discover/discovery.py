"""Discovery engine: pool-first batches, YouTube searches, filters, quota,
topic/modifier selection and learning hooks.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from . import learning
from . import youtube as yt_api

class DiscoveryEngine:
    def __init__(self, db, api, clock=time.time, rng=None,
                 log=None):
        self.db = db
        self.api = api
        self.clock = clock
        self.rng = rng or random.Random()
        self.log = log or (lambda *a, **k: None)

    # ------------------------------------------------------------------ util
    def settings(self) -> dict:
        return self.db.get_settings()

    def now(self) -> float:
        return float(self.clock())

    def quota_status(self, now=None) -> dict:
        settings = self.settings()
        now = now if now is not None else self.now()
        day = yt_api.pacific_day(datetime.fromtimestamp(now, timezone.utc))
        row = self.db.quota_row(day)
        unit_limit = int(settings.get("search_quota_daily_units", 10000))
        call_limit = int(settings.get("search_calls_daily_limit", 100))
        units = int(row["units"])
        calls = int(row["search_calls"])
        ratio = max(units / unit_limit if unit_limit else 0.0,
                    calls / call_limit if call_limit else 0.0)
        return {
            "day": day,
            "units": units,
            "units_limit": unit_limit,
            "search_calls": calls,
            "search_calls_limit": call_limit,
            "videos_calls": int(row["videos_calls"]),
            "ratio": min(1.0, ratio),
            "warn": ratio >= float(settings.get("quota_warn_ratio", 0.8)),
            "exhausted": ratio >= 1.0,
        }

    def quota_allows_search(self) -> tuple[bool, str]:
        status = self.quota_status()
        if status["search_calls"] >= status["search_calls_limit"]:
            return False, ("Daily YouTube search-call limit reached "
                           f"({status['search_calls_limit']} calls). Serving "
                           "from the candidate pool only; quota resets at "
                           "midnight Pacific.")
        if status["units"] + int(self.settings().get("search_cost_units", 100)) > \
                status["units_limit"]:
            return False, ("Daily YouTube API quota exhausted. Serving from the "
                           "candidate pool only; quota resets at midnight "
                           "Pacific.")
        return True, ""

    # --------------------------------------------------------------- filters
    def filter_reason(self, video: dict, settings: dict, seen: set[str],
                      blocked_channels: set[str], blocked_words: set[str],
                      style_filters: list[dict], channel_counts: dict,
                      channel_cap: int) -> str | None:
        vid = video.get("video_id") or ""
        if not yt_api.valid_video_id(vid):
            return "invalid_id"
        if vid in seen:
            return "seen"
        if video.get("channel_id") in blocked_channels:
            return "blocked_channel"
        if video.get("live_broadcast", "none") not in ("", "none"):
            return "live"
        duration = video.get("duration_seconds")
        minimum = int(settings.get("min_duration_seconds", 61))
        if duration is None:
            return "unknown_duration"
        if duration < minimum:
            return "short"
        mode = settings.get("duration_filter", "any")
        threshold = int(settings.get("duration_short_seconds", 1200))
        if mode == "short" and (duration is None or duration > threshold):
            return "duration"
        if mode == "long" and (duration is None or duration < threshold):
            return "duration"
        if settings.get("hidden_gems"):
            views = video.get("views")
            low = int(settings.get("hidden_gems_min_views", 1000))
            high = int(settings.get("hidden_gems_max_views", 100000))
            if views is None or views < low or views > high:
                return "views"
        if learning.matches_blocked_style(video.get("title", ""), style_filters,
                                          settings, blocked_words):
            return "blocked_style"
        if channel_counts.get(video.get("channel_id"), 0) >= channel_cap:
            return "channel_cap"
        return None

    # ------------------------------------------------------------ search prep
    def choose_published_window(self, settings: dict, now: float) -> tuple[str, str]:
        mode = settings.get("published_mode", "any")
        if mode == "last12m":
            after = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=365)
            return yt_api.rfc3339(after), ""
        if self.rng.random() < float(settings.get("published_any_probability", 0.35)):
            return "", ""
        lookback_years = float(settings.get("published_lookback_years", 10))
        min_days = int(settings.get("published_window_min_days", 60))
        max_days = int(settings.get("published_window_max_days", 365))
        if max_days < min_days:
            max_days = min_days
        window = self.rng.randint(min_days, max_days)
        offset = self.rng.uniform(0, max(0.0, lookback_years * 365 - window))
        before_dt = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=offset)
        after_dt = before_dt - timedelta(days=window)
        return yt_api.rfc3339(after_dt), yt_api.rfc3339(before_dt)

    def _lane_topics(self, topics: list[dict], lane: str) -> list[dict]:
        return [t for t in topics if t["lane"] == lane and t["enabled"]]

    def current_daypart(self, settings: dict, now: float) -> dict | None:
        """The enabled time-of-day profile for `now` in local time."""
        if not settings.get("dayparts_enabled", True):
            return None
        try:
            from datetime import datetime as _dt
            hour = _dt.fromtimestamp(now).hour
        except (OverflowError, OSError, ValueError):
            return None
        return learning.active_daypart(self.db.list_dayparts(enabled_only=True),
                                       hour)

    def _intent_of(self, topic: dict) -> str:
        return (topic.get("intent") or "learning").strip().lower()

    def _plan_search(self, settings: dict, now: float,
                     topic_use: dict | None = None,
                     allowed_lanes=None,
                     batch_lanes=()) -> dict | None:
        topics = self.db.list_topics(enabled=True)
        if not topics:
            return None
        events = self.db.list_events()
        half_life = float(settings.get("half_life_days", 14))
        topic_scored = learning.topic_scores(events, now, half_life)
        modifier_scored = learning.modifier_scores(events, now, half_life)
        recent = [s["lane"] for s in self.db.recent_searches(
            int(settings.get("exploration_window", 20)))][::-1]
        profile = self.current_daypart(settings, now)
        intent_weights = (profile or {}).get("intent_weights") or {}
        planner = learning.LanePlanner(
            settings, recent_lanes=recent + list(batch_lanes),
            batch_size=int(settings.get("batch_size", 12)),
            profile=profile)
        lane = planner.choose_lane(self.rng)
        if allowed_lanes:
            lane = self.rng.choice(list(allowed_lanes))

        def lane_pool(name, filtered=True):
            items = self._lane_topics(topics, name)
            if not filtered:
                return items
            return [t for t in items
                    if learning.daypart_allows_intent(profile, self._intent_of(t),
                                                      settings)]

        # Never let a day part produce an empty feed: if it hides every topic,
        # fall back to the full set and say so in the batch messages.
        if profile and not any(lane_pool(name)
                               for name in ("core", "neighbour", "wildcard")):
            self._daypart_note = (
                f"No topic matches the '{profile.get('name')}' day part; "
                "ignoring its intent filter for this batch.")
            profile = None
            intent_weights = {}

        pool = lane_pool(lane)
        if not pool:
            for fallback in ("core", "neighbour", "wildcard"):
                if fallback != lane and (not allowed_lanes or fallback in allowed_lanes):
                    pool = lane_pool(fallback)
                    if pool:
                        lane = fallback
                        break
        if not pool:
            return None
        topic_use = topic_use if topic_use is not None else {}
        allowance = planner.topic_allowance()
        allowed = [t for t in pool if topic_use.get(t["id"], 0) < allowance]
        if not allowed:
            return None
        scored = {}
        for topic in allowed:
            scored[topic["text"]] = topic_scored.get(topic["text"]) or {
                "pos": 0.0, "neg": 0.0, "score": 0.0}
        chosen = learning.thompson_pick(
            allowed, scored, self.rng,
            epsilon=float(settings.get("topic_exploration_epsilon", 0.08)),
            intent_weights=intent_weights or None)
        if chosen is None:
            return None
        topic_use[chosen["id"]] = topic_use.get(chosen["id"], 0) + 1
        modifier = learning.choose_modifier(
            self.db.list_modifiers(), modifier_scored, self.rng,
            float(settings.get("modifier_probability", 0.5)))
        after, before = self.choose_published_window(settings, now)
        orders = settings.get("orders") or ["relevance"]
        order = self.rng.choice(orders)
        return {
            "topic": chosen,
            "lane": lane,
            "modifier": modifier,
            "published_after": after,
            "published_before": before,
            "order": order,
            "query": f"{chosen['text']} {modifier}".strip(),
        }

    # ----------------------------------------------------------------- search
    def _execute_search(self, plan: dict, settings: dict, now: float,
                        seen: set[str], blocked_channels: set[str],
                        blocked_words: set[str], style_filters: list[dict],
                        channel_counts: dict, channel_cap: int) -> list[dict]:
        allowed, message = self.quota_allows_search()
        if not allowed:
            raise yt_api.QuotaExceeded(message)
        results = self.api.search(
            plan["query"],
            published_after=plan["published_after"] or None,
            published_before=plan["published_before"] or None,
            order=plan["order"],
            max_results=int(settings.get("search_max_results", 50)),
            safe_search=settings.get("safe_search", "moderate"),
            relevance_language=settings.get("relevance_language", "en"),
            video_embeddable=True,
        )
        day = yt_api.pacific_day(datetime.fromtimestamp(now, timezone.utc))
        self.db.add_quota(day, units=int(settings.get("search_cost_units", 100)),
                          search_calls=1)
        unseen_ids = [item["video_id"] for item in results
                      if item["video_id"] not in seen
                      and item["channel_id"] not in blocked_channels]
        details = []
        if unseen_ids:
            details = self.api.videos(unseen_ids[:50])
            self.db.add_quota(day, units=int(settings.get("videos_list_cost_units", 1)) *
                              max(1, (len(unseen_ids) + 49) // 50),
                              videos_calls=1)
        merged = {}
        base = {item["video_id"]: item for item in results}
        for item in details:
            merged[item["video_id"]] = {**base.get(item["video_id"], {}), **item}
        added = 0
        accepted = []
        for video in merged.values():
            if self.filter_reason(video, settings, seen, blocked_channels,
                                  blocked_words, style_filters, channel_counts,
                                  channel_cap):
                continue
            self.db.upsert_video({
                **video,
                "topic_text": plan["topic"]["text"],
                "topic_id": plan["topic"]["id"],
                "lane": plan["lane"],
                "modifier": plan["modifier"],
                "first_seen": now,
            })
            self.db.add_pool(video, plan["topic"]["id"], plan["topic"]["text"],
                             plan["lane"], plan["modifier"], now,
                             float(settings.get("pool_expiry_days", 3)))
            channel_counts[video["channel_id"]] = \
                channel_counts.get(video["channel_id"], 0) + 1
            accepted.append(video)
            added += 1
            if len(accepted) >= 50:
                break
        units = int(settings.get("search_cost_units", 100))
        if details:
            units += int(settings.get("videos_list_cost_units", 1)) * \
                max(1, (len(unseen_ids) + 49) // 50)
        self.db.record_search(now, plan["topic"]["text"], plan["topic"]["id"],
                              plan["lane"], plan["modifier"], plan["order"],
                              plan["published_after"], plan["published_before"],
                              units, len(results), added, "normal")
        self.db.bump_topic(plan["topic"]["text"], "searches")
        if plan["modifier"]:
            self.db.bump_modifier(plan["modifier"], "searches")
        if plan["lane"] == "neighbour":
            trial = self.db.get_trial_for_topic(plan["topic"]["id"])
            if trial:
                self.db.bump_trial(plan["topic"]["id"], "searches_used")
        return accepted

    # ------------------------------------------------------------- pool read
    def _take_from_pool(self, need: int, settings: dict, seen: set[str],
                        blocked_channels: set[str], blocked_words: set[str],
                        style_filters: list[dict], channel_counts: dict,
                        channel_cap: int, reserved: set[str] | None = None) -> list[dict]:
        now = self.now()
        self.db.purge_pool(now)
        reserved = reserved if reserved is not None else set()
        taken = []
        for entry in self.db.pool_entries(now):
            if len(taken) >= need:
                break
            video = entry["video"]
            if video.get("video_id") in reserved:
                continue
            if self.filter_reason(video, settings, seen, blocked_channels,
                                  blocked_words, style_filters, channel_counts,
                                  channel_cap):
                continue
            video = dict(video)
            video["topic_text"] = entry["topic_text"]
            video["topic_id"] = entry["topic_id"]
            video["lane"] = entry["lane"]
            video["modifier"] = entry["modifier"]
            video["pooled"] = True
            taken.append(video)
            channel_counts[video["channel_id"]] = \
                channel_counts.get(video["channel_id"], 0) + 1
        return taken

    # ------------------------------------------------------------------ batch
    def fetch_batch(self, count: int | None = None, mode: str = "normal") -> dict:
        settings = self.settings()
        now = self.now()
        self.sweep_impressions(settings)
        batch_size = int(count or settings.get("batch_size", 12))
        batch_size = max(int(settings.get("min_batch_size", 1)),
                         min(int(settings.get("max_batch_size", 30)), batch_size))
        seen = self.db.seen_ids()
        blocked_channels = self.db.blocked_channel_ids()
        blocked_words = self.db.blocked_words()
        style_filters = learning.active_style_filters(self.db)
        channel_cap = int(settings.get("max_per_channel", 2))
        channel_counts: dict[str, int] = {}
        messages: list[str] = []
        collected: list[dict] = []
        reserved: set[str] = set()
        searches = 0
        max_searches = max(1, int(settings.get("max_searches_per_batch", 4)))
        topic_use: dict = {}
        batch_lanes: list[str] = []
        excluded_lanes: set[str] = set()

        if mode != "pool_only":
            collected = self._take_from_pool(
                batch_size, settings, seen, blocked_channels, blocked_words,
                style_filters, channel_counts, channel_cap, reserved)
            reserved.update(v["video_id"] for v in collected)
            if collected:
                messages.append(f"{len(collected)} from candidate pool (0 quota)")

        quota_blocked = False
        while len(collected) < batch_size and searches < max_searches:
            if mode == "wildcard":
                allowed_lanes = ("wildcard",)
            else:
                allowed_lanes = tuple(
                    lane for lane in ("core", "neighbour", "wildcard")
                    if lane not in excluded_lanes)
                if not allowed_lanes:
                    allowed_lanes = ("core", "neighbour", "wildcard")
            plan = self._plan_search(settings, now, topic_use,
                                     allowed_lanes=allowed_lanes,
                                     batch_lanes=batch_lanes)
            note = getattr(self, "_daypart_note", None)
            if note:
                messages.append(note)
                self._daypart_note = None
            if plan is None:
                messages.append(
                    "No enabled topic left under the per-topic ceiling for "
                    "this batch.")
                break
            batch_lanes.append(plan["lane"])
            if len(batch_lanes) >= 2:
                non_core = sum(1 for lane in batch_lanes if lane != "core")
                floor = float(settings.get("exploration_floor", 0.25))
                expected = len(batch_lanes) + 1
                if (non_core + 1) / expected < floor and "core" in allowed_lanes:
                    excluded_lanes.add("core")
            try:
                found = self._execute_search(
                    plan, settings, now, seen, blocked_channels, blocked_words,
                    style_filters, channel_counts, channel_cap)
                searches += 1
                if not found:
                    messages.append(f"No new videos for '{plan['topic']['text']}'.")
            except yt_api.QuotaExceeded as exc:
                quota_blocked = True
                messages.append(str(exc))
                break
            except yt_api.YouTubeError as exc:
                messages.append(str(exc))
                break
            more = self._take_from_pool(
                batch_size - len(collected), settings, seen,
                blocked_channels, blocked_words, style_filters,
                channel_counts, channel_cap, reserved)
            reserved.update(v["video_id"] for v in more)
            collected.extend(more)

        if len(collected) < batch_size and not quota_blocked:
            refill = self._take_from_pool(
                batch_size - len(collected), settings, seen, blocked_channels,
                blocked_words, style_filters, channel_counts, channel_cap,
                reserved)
            reserved.update(v["video_id"] for v in refill)
            collected.extend(refill)

        final = []
        for video in collected:
            if len(final) >= batch_size:
                break
            if video["video_id"] in seen:
                continue
            self.db.mark_seen(video["video_id"], now)
            self.db.add_event(video["video_id"], video.get("topic_text", ""),
                              video.get("channel_id", ""), "impression", 0.0,
                              now, meta=f"lane={video.get('lane','')};"
                                        f"modifier={video.get('modifier','')}")
            if video.get("lane") == "neighbour" and video.get("topic_id"):
                trial = self.db.get_trial_for_topic(video["topic_id"])
                if trial:
                    self.db.bump_trial(video["topic_id"], "impressions")
            self.db.drop_pool(video["video_id"])
            final.append(self._public_video(video))

        if not final:
            messages.append(
                "No new videos matched. Filters or blocked words may be too "
                "strict, or the candidate pool is empty.")
        return {
            "videos": final,
            "messages": messages,
            "quota": self.quota_status(now),
            "searches": searches,
            "pool_remaining": self.db.pool_count(),
            "filters_warning": self.filters_warning(),
        }

    def _public_video(self, video: dict) -> dict:
        age_days = None
        if video.get("published_ts"):
            age_days = max(0.0, (self.now() - video["published_ts"]) / 86400)
        return {
            "video_id": video.get("video_id"),
            "title": video.get("title", ""),
            "channel_id": video.get("channel_id", ""),
            "channel_title": video.get("channel_title", ""),
            "views": video.get("views"),
            "duration_seconds": video.get("duration_seconds"),
            "published_ts": video.get("published_ts") or 0,
            "age_days": age_days,
            "thumbnail": video.get("thumbnail", ""),
            "topic": video.get("topic_text", ""),
            "topic_id": video.get("topic_id"),
            "lane": video.get("lane", ""),
            "modifier": video.get("modifier", ""),
        }

    # --------------------------------------------------------------- learning
    def record_event(self, video_id: str, kind: str, topic_text: str = "",
                     channel_id: str = "", channel_title: str = "",
                     note: str = "") -> dict:
        settings = self.settings()
        valid = {
            "play": settings.get("weight_play", 1.0),
            "more_like_this": settings.get("weight_more_like_this", 3.0),
            "not_this": settings.get("weight_not_this", -1.5),
            "enough_of_this_topic": settings.get("weight_enough_topic", -3.0),
            "block_channel": settings.get("weight_block_channel", 0.0),
        }
        if kind not in valid:
            raise ValueError(f"unknown event kind: {kind}")
        video = self.db.get_video(video_id) if video_id else None
        if video:
            topic_text = topic_text or video.get("topic_text", "")
            channel_id = channel_id or video.get("channel_id", "")
            channel_title = channel_title or video.get("channel_title", "")
        if not yt_api.valid_video_id(video_id or ""):
            video_id = ""
        now = self.now()
        meta_parts = []
        if video and video.get("modifier"):
            meta_parts.append(f"modifier={video['modifier']}")
        if note:
            meta_parts.append(f"note={note}")
        dedupe = kind in ("play", "more_like_this")
        self.db.add_event(video_id or None, topic_text, channel_id, kind,
                          float(valid[kind]), now, meta=";".join(meta_parts),
                          dedupe=dedupe)
        if kind in ("play", "more_like_this"):
            if topic_text:
                self.db.bump_topic(topic_text, "plays")
            if video and video.get("modifier"):
                self.db.bump_modifier(video["modifier"], "plays")
            if video and video.get("topic_id"):
                trial = self.db.get_trial_for_topic(video["topic_id"])
                if trial:
                    self.db.bump_trial(video["topic_id"], "clicks")
        if kind in ("not_this", "enough_of_this_topic") and topic_text:
            self.db.bump_topic(topic_text, "rejects")
        if kind == "block_channel" and yt_api.valid_channel_id(channel_id):
            self.db.block_channel(channel_id, channel_title)
        changed = {"blocked_words": [], "style_filters": [], "retired_trials": []}
        if kind in ("not_this", "enough_of_this_topic"):
            changed["blocked_words"] = learning.learn_words(self.db, now)
            changed["style_filters"] = learning.learn_style(self.db, now)
        if kind in ("play", "more_like_this"):
            learning.propose_trial_topics(self.db, now)
        changed["retired_trials"] = learning.evaluate_trials(self.db, settings)
        self.sweep_impressions(settings)
        return changed

    def sweep_impressions(self, settings: dict | None = None) -> int:
        """Turn old, never-clicked impressions into weak negative events."""
        settings = settings or self.settings()
        after_hours = float(settings.get("impression_miss_after_hours", 24))
        weight = float(settings.get("weight_impression_miss", -0.15))
        now = self.now()
        cutoff = now - after_hours * 3600
        existing = {(e.get("video_id"), e.get("kind")) for e in self.db.list_events()}
        added = 0
        for event in self.db.list_events():
            if event.get("kind") != "impression":
                continue
            if float(event.get("ts") or 0) > cutoff:
                continue
            video_id = event.get("video_id")
            if not video_id:
                continue
            if (video_id, "play") in existing or (video_id, "more_like_this") in existing:
                continue
            if (video_id, "impression_miss") in existing:
                continue
            self.db.add_event(video_id, event.get("topic_text", ""),
                              event.get("channel_id", ""), "impression_miss",
                              weight, now, meta=event.get("meta", ""))
            existing.add((video_id, "impression_miss"))
            added += 1
        return added

    def learn_now(self) -> dict:
        now = self.now()
        settings = self.settings()
        words = learning.learn_words(self.db, now)
        styles = learning.learn_style(self.db, now)
        trials = learning.propose_trial_topics(self.db, now)
        retired = learning.evaluate_trials(self.db, settings)
        self.sweep_impressions(settings)
        return {"blocked_words": words, "style_filters": styles,
                "trial_topics": trials, "retired_trials": retired}

    def filters_warning(self) -> str:
        counts = self.db.counts()
        if counts["pool"] > 0:
            return ""
        blocked = len(self.db.blocked_words())
        styles = len([s for s in self.db.list_style_features() if s["enabled"]])
        if not blocked and not styles:
            return ""
        return (f"The candidate pool is empty with {blocked} blocked word(s) and "
                f"{styles} active title style filter(s); consider unblocking.")
