"""Dynamic learning: decayed event scores, Thompson-sampling lateral choice,
stale-word detection, title-style features and trial-topic lifecycle.

Every weight, share and threshold is read from the settings dict supplied by
the caller (persisted in SQLite). No topic, word, pattern or modifier text is
hardcoded here except the generic stopword fallback from config.py.
"""
from __future__ import annotations

import math
import random
import re

POSITIVE_KINDS = ("play", "more_like_this")
NEGATIVE_KINDS = ("not_this", "enough_of_this_topic")
# Weak negatives affect topic/channel/modifier scores but never auto-block
# words or title styles (those require explicit not_this / enough evidence).
TOPIC_NEGATIVE_KINDS = NEGATIVE_KINDS + ("impression_miss",)

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U0000FE00-\U0000FE0F]+"
)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")


# --------------------------------------------------------------------- decay
def decayed_sum(events, now: float, half_life_days: float) -> float:
    """Sum event weights with exponential decay; <=0 half-life disables decay."""
    if half_life_days <= 0:
        return sum(float(e.get("weight") or 0.0) for e in events)
    tau = half_life_days * 86400.0
    total = 0.0
    for event in events:
        weight = float(event.get("weight") or 0.0)
        age = max(0.0, now - float(event.get("ts") or 0.0))
        total += weight * math.pow(0.5, age / tau)
    return total


def _group(events, key):
    buckets: dict[str, list[dict]] = {}
    for event in events:
        value = (event.get(key) or "").strip()
        if value:
            buckets.setdefault(value, []).append(event)
    return buckets


def _bucket_scores(items, now, half_life_days):
    positives = decayed_sum(
        [e for e in items if e.get("kind") in POSITIVE_KINDS],
        now, half_life_days)
    negatives = decayed_sum(
        [e for e in items if e.get("kind") in TOPIC_NEGATIVE_KINDS],
        now, half_life_days)
    return {"pos": positives, "neg": abs(negatives),
            "score": positives - abs(negatives)}


def topic_scores(events, now: float, half_life_days: float) -> dict:
    return {topic: _bucket_scores(items, now, half_life_days)
            for topic, items in _group(events, "topic_text").items()}


def channel_scores(events, now: float, half_life_days: float) -> dict:
    return {channel: _bucket_scores(items, now, half_life_days)
            for channel, items in _group(events, "channel_id").items()}


def modifier_of(event: dict) -> str:
    for part in (event.get("meta") or "").split(";"):
        if part.startswith("modifier="):
            return part.split("=", 1)[1].strip()
    return ""


def modifier_scores(events, now: float, half_life_days: float) -> dict:
    buckets: dict[str, list[dict]] = {}
    for event in events:
        modifier = modifier_of(event)
        if modifier:
            buckets.setdefault(modifier, []).append(event)
    return {modifier: _bucket_scores(items, now, half_life_days)
            for modifier, items in buckets.items()}


# ------------------------------------------------------------------- bandit
def thompson_pick(candidates, scores, rng: random.Random, prior: float = 1.0,
                  epsilon: float = 0.0, intent_weights: dict | None = None):
    """Beta-Bernoulli style Thompson sampling over decayed evidence.

    alpha = prior + decayed positives, beta = prior + decayed negatives.
    Untried topics start at Beta(1,1) so they are explored immediately.
    `epsilon` is a small uniform-exploration probability so a very strong
    favourite cannot permanently starve the other topics.
    """
    if candidates and epsilon > 0 and rng.random() < epsilon:
        return rng.choice(list(candidates))
    best = None
    best_draw = -1.0
    for candidate in candidates:
        evidence = scores.get(candidate.get("text", "")) or {}
        positive = max(0.0, float(evidence.get("pos") or 0.0))
        negative = max(0.0, float(evidence.get("neg") or 0.0))
        weight = 1.0
        if intent_weights:
            intent = (candidate.get("intent") or "learning").lower()
            weight = float(intent_weights.get(intent, 0.0))
            if weight <= 0:
                continue
        # Weight scales evidence, preserving Beta(1,1) for unknown topics but
        # letting a day part prefer one intent without hiding the others.
        draw = rng.betavariate(prior + positive * weight,
                               prior + negative * weight)
        if draw > best_draw:
            best_draw = draw
            best = candidate
    return best


def active_daypart(dayparts, local_hour: int) -> dict | None:
    """Return the enabled profile whose half-open window contains local_hour.

    Windows may wrap midnight (e.g. 23 -> 6).  The first matching profile in
    start_hour order wins, so overlapping data is predictable and editable.
    """
    for profile in dayparts:
        if not profile.get("enabled", True):
            continue
        start = int(profile.get("start_hour", 0)) % 24
        end = int(profile.get("end_hour", 24))
        hour = local_hour % 24
        if start < end:
            match = start <= hour < end
        elif start > end:
            match = hour >= start or hour < end
        else:
            match = False
        if match:
            return profile
    return None


def daypart_allows_intent(profile: dict | None, intent: str,
                          settings: dict) -> bool:
    """A profile hides intents whose weight is below a small threshold."""
    if not profile:
        return True
    weights = profile.get("intent_weights") or {}
    if not weights:
        return True
    threshold = float(settings.get("daypart_min_intent_weight", 0.05))
    key = (intent or "learning").strip().lower()
    return float(weights.get(key, weights.get("default", 0.0))) >= threshold


def effective_lane_shares(settings: dict, profile: dict | None = None) -> dict:
    core = float(settings.get("lane_core_share", 0.4))
    neighbour = float(settings.get("lane_neighbour_share", 0.4))
    wildcard = float(settings.get("lane_wildcard_share", 0.2))
    if profile:
        core = float(profile.get("lane_core", core))
        neighbour = float(profile.get("lane_neighbour", neighbour))
        wildcard = float(profile.get("lane_wildcard", wildcard))
    explore = min(1.0, max(0.0, float(settings.get("explore", 0.0))))
    if explore > 0:
        moved = core * explore
        core -= moved
        neighbour += moved * 0.5
        wildcard += moved * 0.5
    total = core + neighbour + wildcard
    if total <= 0:
        core, neighbour, wildcard, total = 0.4, 0.4, 0.2, 1.0
    return {"core": core / total, "neighbour": neighbour / total,
            "wildcard": wildcard / total}


class LanePlanner:
    """Chooses lanes for a batch, honouring the exploration floor and topic
    ceiling over a rolling window (default: last 20 executed searches).

    The floor is enforced on the rolling window of previous searches PLUS the
    lanes already planned for this batch, so a run of batches cannot starve
    neighbour/wildcard lanes.
    """

    LANES = ("core", "neighbour", "wildcard")

    def __init__(self, settings: dict, recent_lanes=(), batch_size: int = 12,
                 profile: dict | None = None):
        self.settings = settings
        self.batch_size = max(1, int(batch_size))
        self.window = max(1, int(settings.get("exploration_window", 20)))
        self.floor = min(1.0, max(0.0, float(
            settings.get("exploration_floor", 0.25))))
        self.ceiling = min(1.0, max(0.05, float(
            settings.get("topic_ceiling", 0.3))))
        self.max_searches = max(1, int(settings.get(
            "max_searches_per_batch", 4)))
        self.shares = effective_lane_shares(settings, profile)
        self.recent = list(recent_lanes)[-self.window:]
        self.planned: list[str] = []

    def _exploration_ratio(self, lanes) -> float:
        if not lanes:
            return self.shares["neighbour"] + self.shares["wildcard"]
        return sum(1 for lane in lanes if lane != "core") / len(lanes)

    def choose_lane(self, rng: random.Random) -> str:
        combined = (self.recent + self.planned)[-self.window:]
        if self._exploration_ratio(combined) < self.floor:
            neighbour = self.shares["neighbour"]
            wildcard = self.shares["wildcard"]
            total = neighbour + wildcard
            lane = ("neighbour" if rng.random() < neighbour / total
                    else "wildcard") if total > 0 else "wildcard"
        else:
            lane = rng.choices(self.LANES,
                               weights=[self.shares[l] for l in self.LANES])[0]
        self.planned.append(lane)
        self.recent.append(lane)
        self.recent = self.recent[-self.window:]
        return lane

    def topic_allowance(self) -> int:
        """Max searches a single topic may take in this batch.

        Floor (not ceil) so the configured ceiling is never exceeded: with
        4 searches and 0.3, a topic may appear once (25%).
        """
        searches = min(self.batch_size, self.max_searches)
        return max(1, int(math.floor(searches * self.ceiling)))


def choose_modifier(modifiers, scores, rng, probability: float):
    enabled = [m for m in modifiers if m.get("enabled")]
    if not enabled or rng.random() >= probability:
        return ""
    ranked = []
    for modifier in enabled:
        evidence = scores.get(modifier["text"]) or {}
        ranked.append((evidence.get("score", 0.0), modifier))
    if not ranked:
        return ""
    explored = rng.random() < 0.2
    if explored:
        return rng.choice(enabled)["text"]
    best_score = max(score for score, _ in ranked)
    top = [m["text"] for score, m in ranked if score == best_score]
    return rng.choice(top)


# --------------------------------------------------------------- word learning
def tokenize_title(title: str, stopwords) -> list[str]:
    text = (title or "").lower()
    words = WORD_RE.findall(text)
    stops = {str(word).lower() for word in (stopwords or [])}
    return [word for word in words
            if len(word) >= 3 and word not in stops]


def word_evidence(events, videos_by_id, stopwords, now, half_life_days) -> dict:
    """Per-word positive/negative decayed evidence and distinct video counts."""
    stats: dict[str, dict] = {}

    def bucket(word):
        return stats.setdefault(word, {
            "pos": 0.0, "neg": 0.0, "pos_videos": set(), "neg_videos": set(),
        })

    for event in events:
        kind = event.get("kind")
        if kind not in POSITIVE_KINDS and kind not in NEGATIVE_KINDS:
            continue
        video = videos_by_id.get(event.get("video_id") or "")
        if not video:
            continue
        words = set(tokenize_title(video.get("title", ""), stopwords))
        for word in words:
            entry = bucket(word)
            if kind in POSITIVE_KINDS:
                entry["pos"] += float(event.get("weight") or 0.0) * _decay(
                    now, event.get("ts"), half_life_days)
                entry["pos_videos"].add(video["video_id"])
            else:
                entry["neg"] += abs(float(event.get("weight") or 0.0)) * _decay(
                    now, event.get("ts"), half_life_days)
                entry["neg_videos"].add(video["video_id"])
    for entry in stats.values():
        total = entry["pos"] + entry["neg"]
        entry["negative_share"] = entry["neg"] / total if total > 0 else 0.0
        entry["score"] = entry["pos"] - entry["neg"]
        entry["distinct_negatives"] = len(entry["neg_videos"])
        entry["distinct_positives"] = len(entry["pos_videos"])
        entry["pos_videos"] = sorted(entry["pos_videos"])
        entry["neg_videos"] = sorted(entry["neg_videos"])
    return stats


def _decay(now, ts, half_life_days):
    if half_life_days <= 0:
        return 1.0
    age = max(0.0, now - float(ts or 0.0))
    return math.pow(0.5, age / (half_life_days * 86400.0))


def should_auto_block(entry: dict, settings: dict) -> bool:
    return (
        int(entry.get("distinct_negatives", 0))
        >= int(settings.get("word_min_distinct_negatives", 3))
        and float(entry.get("negative_share", 0.0))
        >= float(settings.get("word_negative_share", 0.8))
    )


def learn_words(db, now: float) -> list[str]:
    """Auto-block qualifying words (respecting protection and the cap)."""
    settings = db.get_settings()
    videos = {v["video_id"]: v for v in db.list_videos()}
    events = db.list_events()
    evidence = word_evidence(events, videos, settings.get("stopwords"),
                             now, float(settings.get("half_life_days", 14)))
    existing = {row["word"].lower(): row for row in db.list_avoid_words()}
    cap = int(settings.get("max_auto_blocked_words", 100))
    auto_count = sum(1 for row in existing.values() if row["auto"] and row["blocked"])
    added = []
    for word, entry in sorted(evidence.items(),
                              key=lambda item: item[1]["negative_share"],
                              reverse=True):
        if word in existing:
            continue
        if not should_auto_block(entry, settings):
            continue
        if auto_count >= cap:
            break
        if db.add_avoid_word(word, source="auto", auto=True):
            added.append(word)
            auto_count += 1
    return added


# -------------------------------------------------------------- style learning
def caps_ratio(title: str) -> float:
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def emoji_count(title: str) -> int:
    return len(EMOJI_RE.findall(title or ""))


def feature_key(kind: str, pattern: str = "") -> str:
    return f"{kind}:{pattern}" if pattern else kind


def feature_matches_title(title: str, feature: dict, settings: dict) -> bool:
    kind = feature.get("kind")
    if kind == "caps":
        return caps_ratio(title) >= float(settings.get("style_caps_ratio", 0.7))
    if kind == "emoji":
        return emoji_count(title) >= int(settings.get("style_emoji_min", 3))
    if kind == "regex":
        try:
            return re.search(feature.get("pattern") or "", title or "") is not None
        except re.error:
            return False
    return False


def style_evidence(events, videos_by_id, features, settings, now,
                   half_life_days) -> dict:
    stats: dict[str, dict] = {}
    for feature in features:
        key = feature_key(feature["kind"], feature.get("pattern", ""))
        stats[key] = {"pos": 0.0, "neg": 0.0, "pos_videos": set(),
                      "neg_videos": set(), "feature": feature}
    for event in events:
        kind = event.get("kind")
        if kind not in POSITIVE_KINDS and kind not in NEGATIVE_KINDS:
            continue
        video = videos_by_id.get(event.get("video_id") or "")
        if not video:
            continue
        for feature in features:
            if not feature_matches_title(video.get("title", ""), feature, settings):
                continue
            key = feature_key(feature["kind"], feature.get("pattern", ""))
            entry = stats[key]
            decay = _decay(now, event.get("ts"), half_life_days)
            if kind in POSITIVE_KINDS:
                entry["pos"] += float(event.get("weight") or 0.0) * decay
                entry["pos_videos"].add(video["video_id"])
            else:
                entry["neg"] += abs(float(event.get("weight") or 0.0)) * decay
                entry["neg_videos"].add(video["video_id"])
    for entry in stats.values():
        total = entry["pos"] + entry["neg"]
        entry["negative_share"] = entry["neg"] / total if total > 0 else 0.0
        entry["score"] = entry["pos"] - entry["neg"]
        entry["distinct_negatives"] = len(entry["neg_videos"])
        entry["pos_videos"] = sorted(entry["pos_videos"])
        entry["neg_videos"] = sorted(entry["neg_videos"])
    return stats


def learn_style(db, now: float) -> list[dict]:
    settings = db.get_settings()
    videos = {v["video_id"]: v for v in db.list_videos()}
    features = db.list_style_features()
    evidence_features = list(features)
    for kind in ("caps", "emoji"):
        if not any(f["kind"] == kind and not f.get("pattern") for f in features):
            evidence_features.append({"kind": kind, "pattern": ""})
    evidence = style_evidence(db.list_events(), videos, evidence_features,
                              settings, now,
                              float(settings.get("half_life_days", 14)))
    created = []
    min_neg = int(settings.get("style_min_distinct_negatives", 3))
    share = float(settings.get("style_negative_share", 0.8))
    enabled = [f for f in features if f["enabled"]]
    auto_count = sum(1 for f in features if f["auto"])
    cap = int(settings.get("max_auto_style_filters", 20))

    builtin = [
        ("caps", ""),
        ("emoji", ""),
    ]
    for kind, pattern in builtin:
        key = feature_key(kind, pattern)
        if any(f["kind"] == kind and not f.get("pattern") for f in features):
            continue
        entry = evidence.get(key)
        if not entry:
            continue
        if entry["distinct_negatives"] >= min_neg and entry["negative_share"] >= share:
            if auto_count >= cap:
                break
            if db.add_style_feature(kind, "", source="auto", auto=True):
                created.append({"kind": kind, "pattern": ""})
                auto_count += 1

    for feature in enabled:
        if feature.get("auto"):
            continue
        key = feature_key(feature["kind"], feature.get("pattern", ""))
        entry = evidence.get(key) or {}
        if entry.get("distinct_negatives", 0) >= min_neg and \
                entry.get("negative_share", 0) >= share:
            db.set_style_feature(feature["id"], False)
    return created


def active_style_filters(db) -> list[dict]:
    return [f for f in db.list_style_features() if f["enabled"]]


def matches_blocked_style(title: str, features, settings, blocked_words) -> bool:
    lowered = (title or "").lower()
    for word in blocked_words:
        if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", lowered):
            return True
    for feature in features:
        if feature_matches_title(title, feature, settings):
            return True
    return False


# ------------------------------------------------------------- trial topics
def propose_trial_topics(db, now: float) -> list[str]:
    """Derive candidate neighbour topics from videos the user liked.

    Sources: snippet.tags plus distinctive title keywords, excluding any word
    already blocked or already present as a topic. No language model involved.
    """
    settings = db.get_settings()
    cap = int(settings.get("max_trial_topics", 5))
    per_refresh = int(settings.get("trial_max_new_per_refresh", 3))
    min_len = int(settings.get("trial_min_keyword_length", 4))
    budget = int(settings.get("trial_budget_searches", 5))
    if len(db.list_trials("active")) >= cap:
        return []

    liked_ids = []
    for event in db.list_events():
        if event.get("kind") in ("play", "more_like_this") and event.get("video_id"):
            liked_ids.append(event["video_id"])
    if not liked_ids:
        return []

    existing_topics = {t["text"].lower() for t in db.list_topics()}
    blocked = db.blocked_words()
    stopwords = {str(w).lower() for w in (settings.get("stopwords") or [])}
    videos = {v["video_id"]: v for v in db.list_videos()}

    candidates: list[tuple[float, str, str]] = []
    seen: set[str] = set()
    for video_id in dict.fromkeys(reversed(liked_ids)):
        video = videos.get(video_id)
        if not video:
            continue
        for tag in video.get("tags") or []:
            phrase = " ".join(str(tag).split())
            low = phrase.lower()
            if len(low) < min_len or low in seen or low in existing_topics:
                continue
            if any(word in blocked for word in low.split()):
                continue
            seen.add(low)
            candidates.append((2.0, phrase, video_id))
        words = [w for w in tokenize_title(video.get("title", ""), stopwords)
                 if len(w) >= min_len]
        words.sort(key=len, reverse=True)
        for word in words[:2]:
            low = word.lower()
            if low in seen or low in existing_topics:
                continue
            seen.add(low)
            candidates.append((1.0, word, video_id))

    created = []
    active_count = len(db.list_trials("active"))
    for _, phrase, video_id in candidates:
        if len(created) >= per_refresh or active_count + len(created) >= cap:
            break
        topic_id = db.add_topic(phrase, "neighbour", source="trial")
        if not topic_id:
            continue
        db.add_trial(topic_id, phrase, budget, video_id, now)
        created.append(phrase)
        active_count += 1
    return created


def evaluate_trials(db, settings: dict) -> list[dict]:
    """Retire trials that perform poorly; promote ones that earn their keep.

    A trial is only judged after its search budget is spent AND it has had
    `trial_min_impressions` impressions.  Below the click-rate threshold it is
    retired (topic disabled, revivable in the UI).  At or above it the topic is
    promoted to an ordinary neighbour topic so it stops occupying a trial slot.
    """
    retired = []
    minimum = int(settings.get("trial_min_impressions", 10))
    threshold = float(settings.get("trial_retire_click_rate", 0.05))
    for trial in db.list_trials("active"):
        if trial["searches_used"] < trial["budget"]:
            continue
        if trial["impressions"] < minimum:
            continue
        click_rate = (trial["clicks"] / trial["impressions"]
                      if trial["impressions"] else 0.0)
        if click_rate < threshold:
            db.update_trial(trial["id"], status="retired")
            db.set_topic_enabled(trial["topic_id"], False)
            retired.append(trial["text"])
        else:
            db.update_trial(trial["id"], status="promoted")
    return retired


def revive_trial(db, topic_id: int) -> bool:
    trial = db.get_trial_for_topic(topic_id)
    if not trial or trial["status"] != "retired":
        return False
    db.update_trial(trial["id"], status="active", searches_used=0,
                    impressions=0, clicks=0)
    db.set_topic_enabled(topic_id, True)
    return True
