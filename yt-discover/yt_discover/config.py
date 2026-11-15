"""Configuration defaults, filesystem locations, and API-key loading.

All numeric/textual tuning lives here (or, after first run, in the settings
table).  No topics, modifiers, blocked words or title patterns live in code.
The only word list in code is the generic English stopword list below; it is
copied into the settings table on first run and can be overridden there.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

APP_NAME = "yt-discover"
SCHEMA_VERSION = 1

DEFAULT_STOPWORDS = [
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been",
    "before", "being", "below", "between", "both", "but", "by", "can",
    "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does",
    "doesn't", "doing", "don't", "down", "during", "each", "few", "for",
    "from", "further", "had", "hadn't", "has", "hasn't", "have", "haven't",
    "having", "he", "he'd", "he'll", "he's", "her", "here", "here's",
    "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd",
    "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's",
    "its", "itself", "let's", "me", "more", "most", "mustn't", "my",
    "myself", "no", "nor", "not", "of", "off", "on", "once", "only", "or",
    "other", "ought", "our", "ours", "ourselves", "out", "over", "own",
    "same", "shan't", "she", "she'd", "she'll", "she's", "should",
    "shouldn't", "so", "some", "such", "than", "that", "that's", "the",
    "their", "theirs", "them", "themselves", "then", "there", "there's",
    "these", "they", "they'd", "they'll", "they're", "they've", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't",
    "what", "what's", "when", "when's", "where", "where's", "which",
    "while", "who", "who's", "whom", "why", "why's", "with", "won't",
    "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've",
    "your", "yours", "yourself", "yourselves",
]

# Details I could not fully confirm on 2026-11-14. Kept as data, not logic.
TODO_UNVERIFIED = [
    "search.list quota: the current quota page documents a separate 100-calls/day "
    "search bucket at 1 unit per call, while older docs said 100 units per call. "
    "This app tracks both rails; confirm against "
    "https://developers.google.com/youtube/v3/determine_quota_cost",
    "Whether a billing account/credit card is required for the free API quota in "
    "your Google Cloud project. Try without billing first.",
    "videoDuration is documented for search.list but this app filters duration "
    "client-side from videos.list contentDetails.duration, so it is not sent.",
    "relatedToVideoId was removed on 2023-08-07 and is intentionally not used.",
]

# Time-of-day profiles. Windows are half-open [start_hour, end_hour) in LOCAL
# time and may wrap midnight. Topics carry an `intent` tag; a profile decides
# which intents are visible via `intent_weights` (>= daypart_min_intent_weight
# means allowed) and may override the lane shares. All of this is data: edit it
# in the Tune panel or seed.json. Windows/names below are only defaults.
DEFAULT_DAYPARTS = [
    {
        "name": "Learning day",
        "start_hour": 6,
        "end_hour": 19,
        "lane_core": 0.55,
        "lane_neighbour": 0.30,
        "lane_wildcard": 0.15,
        "intent_weights": {"learning": 1.0, "fun": 0.0},
    },
    {
        "name": "Evening fun",
        "start_hour": 19,
        "end_hour": 23,
        "lane_core": 0.45,
        "lane_neighbour": 0.25,
        "lane_wildcard": 0.30,
        "intent_weights": {"fun": 1.0, "learning": 0.0},
    },
    {
        "name": "Late night",
        "start_hour": 23,
        "end_hour": 6,
        "lane_core": 0.35,
        "lane_neighbour": 0.25,
        "lane_wildcard": 0.40,
        "intent_weights": {"fun": 1.0, "learning": 0.0},
    },
]

DEFAULTS = {
    # server / batch
    "server_port": 8765,
    "batch_size": 12,
    "min_batch_size": 1,
    "max_batch_size": 30,
    "max_searches_per_batch": 4,

    # YouTube API
    "search_max_results": 50,
    "safe_search": "moderate",          # none | moderate | strict
    "relevance_language": "en",
    "orders": ["relevance", "date", "viewCount", "rating"],
    "published_mode": "any",            # any | last12m
    "published_any_probability": 0.35,
    "published_lookback_years": 10,
    "published_window_min_days": 60,
    "published_window_max_days": 365,

    # quota (units model + search-call bucket rail)
    "search_quota_daily_units": 10000,
    "search_cost_units": 100,
    "videos_list_cost_units": 1,
    "search_calls_daily_limit": 100,
    "quota_warn_ratio": 0.8,

    # day parts (time-of-day profiles; windows are local time)
    "daypart_min_intent_weight": 0.05,
    "dayparts_enabled": True,

    # lanes
    "lane_core_share": 0.4,
    "lane_neighbour_share": 0.4,
    "lane_wildcard_share": 0.2,
    "explore": 0.0,
    "exploration_floor": 0.25,
    "exploration_window": 20,
    "topic_ceiling": 0.3,
    "topic_exploration_epsilon": 0.08,

    # filters
    "max_per_channel": 2,
    "min_duration_seconds": 61,
    "duration_filter": "any",           # any | short | long
    "duration_short_seconds": 1200,
    "hidden_gems": False,
    "hidden_gems_min_views": 1000,
    "hidden_gems_max_views": 100000,
    "pool_expiry_days": 3,

    # learning
    "half_life_days": 14,
    "modifier_probability": 0.5,
    "weight_play": 1.0,
    "weight_more_like_this": 3.0,
    "weight_not_this": -1.5,
    "weight_enough_topic": -3.0,
    "weight_block_channel": 0.0,
    "weight_impression_miss": -0.15,
    "impression_miss_after_hours": 24,

    # stale-word learning
    "word_min_distinct_negatives": 3,
    "word_negative_share": 0.8,
    "max_auto_blocked_words": 100,

    # style learning
    "style_caps_ratio": 0.7,
    "style_emoji_min": 3,
    "style_min_distinct_negatives": 3,
    "style_negative_share": 0.8,
    "max_auto_style_filters": 20,

    # trial topics
    "max_trial_topics": 5,
    "trial_budget_searches": 5,
    "trial_min_impressions": 10,
    "trial_retire_click_rate": 0.05,
    "trial_max_new_per_refresh": 3,
    "trial_min_keyword_length": 4,

    # tokenization (overridable data)
    "stopwords": DEFAULT_STOPWORDS,
}


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def data_dir() -> Path:
    return _env_path("YT_DISCOVER_DATA_DIR") or (Path.home() / ".local" / "share" / APP_NAME)


def config_dir() -> Path:
    return _env_path("YT_DISCOVER_CONFIG_DIR") or (Path.home() / ".config" / APP_NAME)


def state_dir() -> Path:
    return _env_path("YT_DISCOVER_STATE_DIR") or (Path.home() / ".local" / "state" / APP_NAME)


def db_path() -> Path:
    return data_dir() / "discover.db"


def env_file_path() -> Path:
    return config_dir() / "env"


def seed_path() -> Path | None:
    override = _env_path("YT_DISCOVER_SEED")
    if override:
        return override
    for candidate in (config_dir() / "seed.json", Path.cwd() / "seed.json"):
        if candidate.is_file():
            return candidate
    return None


def read_env_file(path: Path | None = None) -> dict:
    """Parse the tiny KEY=VALUE env file without executing it."""
    path = path or env_file_path()
    values = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def load_api_key() -> str | None:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    key = read_env_file().get("YOUTUBE_API_KEY", "").strip()
    return key or None


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_SECRET_PARAM_RE = re.compile(r"([?&](?:key|token|api_key)=)[^&\s]+",
                              re.IGNORECASE)


def sanitize_error(message: str, api_key: str | None = None) -> str:
    """Strip URLs, key-like tokens and the API key from error messages."""
    text = str(message)
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = _SECRET_PARAM_RE.sub(r"\1[redacted]", text)
    text = _GOOGLE_KEY_RE.sub("[redacted]", text)
    text = _URL_RE.sub("[url removed]", text)
    return text[:400]


def json_setting(value) -> str:
    return json.dumps(value, ensure_ascii=False)
