"""SQLite persistence for yt-discover.

One connection is shared by all threads and guarded by a re-entrant lock.
WAL mode keeps the UI responsive while the watcher starts/stops the server.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config

TABLES = [
    "settings", "topics", "modifiers", "blocked_channels", "avoid_words",
    "style_features", "videos", "seen", "events", "pool", "trial_topics",
    "searches", "quota", "dayparts",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('core','neighbour','wildcard')),
    intent TEXT NOT NULL DEFAULT 'learning',
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'user',
    protected INTEGER NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0,
    plays INTEGER NOT NULL DEFAULT 0,
    rejects INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE (text, lane)
);
CREATE TABLE IF NOT EXISTS modifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'user',
    protected INTEGER NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0,
    plays INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blocked_channels (
    channel_id TEXT PRIMARY KEY,
    channel_title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS avoid_words (
    word TEXT PRIMARY KEY COLLATE NOCASE,
    source TEXT NOT NULL DEFAULT 'user',
    blocked INTEGER NOT NULL DEFAULT 1,
    protected INTEGER NOT NULL DEFAULT 0,
    auto INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS style_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('caps','emoji','regex')),
    pattern TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user',
    enabled INTEGER NOT NULL DEFAULT 1,
    protected INTEGER NOT NULL DEFAULT 0,
    auto INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE (kind, pattern)
);
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_title TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    published_ts REAL NOT NULL DEFAULT 0,
    duration_seconds INTEGER,
    views INTEGER,
    tags TEXT NOT NULL DEFAULT '[]',
    topic_text TEXT NOT NULL DEFAULT '',
    topic_id INTEGER,
    lane TEXT NOT NULL DEFAULT '',
    modifier TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id),
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    video_id TEXT,
    topic_text TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL UNIQUE,
    added_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    topic_text TEXT NOT NULL DEFAULT '',
    topic_id INTEGER,
    lane TEXT NOT NULL DEFAULT '',
    modifier TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trial_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL UNIQUE,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_ts REAL NOT NULL,
    parent_video TEXT NOT NULL DEFAULT '',
    searches_used INTEGER NOT NULL DEFAULT 0,
    budget INTEGER NOT NULL DEFAULT 5,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    topic_text TEXT NOT NULL DEFAULT '',
    topic_id INTEGER,
    lane TEXT NOT NULL DEFAULT '',
    modifier TEXT NOT NULL DEFAULT '',
    order_by TEXT NOT NULL DEFAULT '',
    published_after TEXT NOT NULL DEFAULT '',
    published_before TEXT NOT NULL DEFAULT '',
    units INTEGER NOT NULL DEFAULT 0,
    results INTEGER NOT NULL DEFAULT 0,
    new_candidates INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'normal'
);
CREATE TABLE IF NOT EXISTS dayparts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_hour INTEGER NOT NULL CHECK (start_hour BETWEEN 0 AND 23),
    end_hour INTEGER NOT NULL CHECK (end_hour BETWEEN 0 AND 24),
    lane_core REAL NOT NULL DEFAULT 0.4,
    lane_neighbour REAL NOT NULL DEFAULT 0.4,
    lane_wildcard REAL NOT NULL DEFAULT 0.2,
    intent_weights TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'default',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    day TEXT PRIMARY KEY,
    units INTEGER NOT NULL DEFAULT 0,
    search_calls INTEGER NOT NULL DEFAULT 0,
    videos_calls INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_video ON events(video_id);
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen(ts);
CREATE INDEX IF NOT EXISTS idx_pool_expiry ON pool(expires_ts);
CREATE INDEX IF NOT EXISTS idx_searches_ts ON searches(ts);
"""

VIDEO_COLUMNS = [
    "video_id", "title", "channel_id", "channel_title", "published_at",
    "published_ts", "duration_seconds", "views", "tags", "topic_text",
    "topic_id", "lane", "modifier", "first_seen",
]
TOPIC_COLUMNS = [
    "id", "text", "lane", "intent", "enabled", "source", "protected",
    "searches", "plays", "rejects", "created_at",
]
MODIFIER_COLUMNS = [
    "id", "text", "enabled", "source", "protected", "searches", "plays",
    "created_at",
]
EVENT_COLUMNS = [
    "id", "ts", "video_id", "topic_text", "channel_id", "kind", "weight",
    "meta",
]


class Database:
    def __init__(self, path: Path | str, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._migrate()
        self._first_run()

    # --------------------------------------------------------------- migrate
    def _migrate(self) -> None:
        """Additive migrations for databases created before this version."""
        with self.lock:
            columns = {row["name"] for row in
                       self._rows("PRAGMA table_info(topics)")}
            if "intent" not in columns:
                self.conn.execute(
                    "ALTER TABLE topics ADD COLUMN intent TEXT NOT NULL "
                    "DEFAULT 'learning'")
                self.conn.commit()
            if not self._one("SELECT 1 FROM dayparts LIMIT 1"):
                for profile in config.DEFAULT_DAYPARTS:
                    self._insert_daypart(profile, source="default")
                self.conn.commit()

    def _insert_daypart(self, profile: dict, source: str = "default") -> int:
        return self._write(
            "INSERT INTO dayparts(name,start_hour,end_hour,lane_core,"
            "lane_neighbour,lane_wildcard,intent_weights,enabled,source,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(profile.get("name") or "Unnamed"),
             int(profile.get("start_hour", 0)),
             int(profile.get("end_hour", 24)),
             float(profile.get("lane_core", 0.4)),
             float(profile.get("lane_neighbour", 0.4)),
             float(profile.get("lane_wildcard", 0.2)),
             config.json_setting(profile.get("intent_weights") or {}),
             int(profile.get("enabled", True)),
             source, self.clock()),
        )

    # ---------------------------------------------------------------- basics
    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _rows(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            cur = self.conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _one(self, sql: str, params=()):
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _write(self, sql: str, params=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def _first_run(self) -> None:
        if not self.get_meta("initialized"):
            for key, value in config.DEFAULTS.items():
                if self.get_setting(key, _sentinel=True) is _MISSING:
                    self.set_setting(key, value)
            self.set_meta("initialized", "1")
            self.set_meta("schema_version", str(config.SCHEMA_VERSION))
        self._maybe_apply_seed()

    def reload_seed(self, path: Path | None = None) -> dict:
        """Re-apply seed.json additively (never deletes or overwrites rows).

        Topics/modifiers/words that already exist are skipped, so this is safe
        to repeat after you edit the file. Settings and dayparts already present
        are left alone.
        """
        target = path or config.seed_path()
        if not target:
            return {"ok": False, "reason": "no seed.json found"}
        try:
            seed = json.loads(Path(target).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": f"cannot read seed: {exc}"}
        if not isinstance(seed, dict):
            return {"ok": False, "reason": "seed is not an object"}
        before = self.counts()
        self.apply_seed(seed, source="seed")
        after = self.counts()
        added = {table: after[table] - before[table] for table in after
                 if after[table] != before[table]}
        return {"ok": True, "path": str(target), "added": added}

    def _maybe_apply_seed(self) -> None:
        """Import seed.json exactly once; later edits never resurrect entries
        you deleted. Use Export/Import or the UI for subsequent changes."""
        if self.get_meta("seed_applied"):
            return
        path = config.seed_path()
        if not path:
            return
        try:
            seed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(seed, dict):
            self.apply_seed(seed, source="seed")
            self.set_meta("seed_applied", str(path))

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default=None):
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default=None, _sentinel: bool = False):
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        if row:
            try:
                return json.loads(row["value"])
            except ValueError:
                return row["value"]
        if _sentinel:
            return _MISSING
        return config.DEFAULTS.get(key, default)

    def get_settings(self) -> dict:
        values = dict(config.DEFAULTS)
        for row in self._rows("SELECT key,value FROM settings"):
            try:
                values[row["key"]] = json.loads(row["value"])
            except ValueError:
                values[row["key"]] = row["value"]
        return values

    def set_setting(self, key: str, value) -> None:
        if key not in config.DEFAULTS:
            raise ValueError(f"unknown setting: {key}")
        self._write(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, config.json_setting(value)),
        )

    def set_settings(self, values: dict) -> None:
        for key, value in values.items():
            self.set_setting(key, value)

    # ---------------------------------------------------------------- topics
    def add_topic(self, text: str, lane: str, source: str = "user",
                  enabled: bool = True, intent: str = "learning") -> int | None:
        text = " ".join(str(text).split())
        if not text:
            return None
        if lane not in ("core", "neighbour", "wildcard"):
            raise ValueError("lane must be core, neighbour or wildcard")
        intent = " ".join(str(intent or "learning").split()).lower()
        if not intent:
            intent = "learning"
        try:
            return self._write(
                "INSERT INTO topics(text,lane,intent,enabled,source,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (text, lane, intent, int(enabled), source, self.clock()),
            )
        except sqlite3.IntegrityError:
            return None

    def list_topics(self, lane: str | None = None, enabled: bool | None = None,
                    source: str | None = None) -> list[dict]:
        sql = "SELECT * FROM topics WHERE 1=1"
        params: list = []
        if lane:
            sql += " AND lane=?"
            params.append(lane)
        if enabled is not None:
            sql += " AND enabled=?"
            params.append(int(enabled))
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY lane, text COLLATE NOCASE"
        rows = self._rows(sql, params)
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["protected"] = bool(row["protected"])
        return rows

    def remove_topic(self, topic_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM trial_topics WHERE topic_id=?", (topic_id,))
            self.conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
            self.conn.commit()

    def set_topic_enabled(self, topic_id: int, enabled: bool) -> None:
        self._write("UPDATE topics SET enabled=? WHERE id=?", (int(enabled), topic_id))

    def set_topic_intent(self, topic_id: int, intent: str) -> None:
        value = " ".join(str(intent or "learning").split()).lower() or "learning"
        self._write("UPDATE topics SET intent=? WHERE id=?", (value, topic_id))

    def topic_intents(self) -> list[str]:
        return [row["intent"] for row in self._rows(
            "SELECT DISTINCT intent FROM topics ORDER BY intent")]

    def bump_topic(self, topic_text: str, field: str) -> None:
        if field not in ("searches", "plays", "rejects"):
            raise ValueError(field)
        self._write(
            f"UPDATE topics SET {field}={field}+1 WHERE text=?",
            (topic_text,),
        )

    # -------------------------------------------------------------- dayparts
    def list_dayparts(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM dayparts"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY start_hour, id"
        rows = self._rows(sql)
        for row in rows:
            try:
                row["intent_weights"] = json.loads(row["intent_weights"])
            except (ValueError, TypeError):
                row["intent_weights"] = {}
            if not isinstance(row["intent_weights"], dict):
                row["intent_weights"] = {}
            row["enabled"] = bool(row["enabled"])
        return rows

    def daypart(self, daypart_id: int) -> dict | None:
        return next((p for p in self.list_dayparts()
                     if p["id"] == daypart_id), None)

    def set_daypart_enabled(self, daypart_id: int, enabled: bool) -> None:
        self._write("UPDATE dayparts SET enabled=? WHERE id=?",
                    (int(enabled), daypart_id))

    def update_daypart(self, daypart_id: int, **fields) -> None:
        allowed = {"name", "start_hour", "end_hour", "lane_core",
                   "lane_neighbour", "lane_wildcard", "intent_weights",
                   "enabled"}
        values = {}
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown daypart field: {key}")
            if key == "intent_weights":
                value = config.json_setting(value or {})
            elif key in ("start_hour", "end_hour"):
                value = int(value)
            elif key.startswith("lane_"):
                value = float(value)
            elif key == "enabled":
                value = int(bool(value))
            values[key] = value
        if not values:
            return
        assignments = ", ".join(f"{key}=?" for key in values)
        self._write(f"UPDATE dayparts SET {assignments} WHERE id=?",
                    (*values.values(), daypart_id))

    def add_daypart(self, profile: dict, source: str = "user") -> int:
        return self._insert_daypart(profile, source=source)

    def remove_daypart(self, daypart_id: int) -> None:
        self._write("DELETE FROM dayparts WHERE id=?", (daypart_id,))

    # ------------------------------------------------------------- modifiers
    def add_modifier(self, text: str, source: str = "user") -> int | None:
        text = " ".join(str(text).split())
        if not text:
            return None
        try:
            return self._write(
                "INSERT INTO modifiers(text,source,created_at) VALUES(?,?,?)",
                (text, source, self.clock()),
            )
        except sqlite3.IntegrityError:
            return None

    def list_modifiers(self) -> list[dict]:
        rows = self._rows("SELECT * FROM modifiers ORDER BY text COLLATE NOCASE")
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["protected"] = bool(row["protected"])
        return rows

    def remove_modifier(self, modifier_id: int) -> None:
        self._write("DELETE FROM modifiers WHERE id=?", (modifier_id,))

    def bump_modifier(self, text: str, field: str) -> None:
        if field not in ("searches", "plays"):
            raise ValueError(field)
        self._write(
            f"UPDATE modifiers SET {field}={field}+1 WHERE text=?", (text,)
        )

    # ------------------------------------------------------- blocked channels
    def block_channel(self, channel_id: str, title: str = "",
                      source: str = "user") -> None:
        self._write(
            "INSERT INTO blocked_channels(channel_id,channel_title,source,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
            "channel_title=excluded.channel_title",
            (channel_id, title, source, self.clock()),
        )

    def unblock_channel(self, channel_id: str) -> None:
        self._write("DELETE FROM blocked_channels WHERE channel_id=?", (channel_id,))

    def blocked_channel_ids(self) -> set[str]:
        return {r["channel_id"] for r in self._rows("SELECT channel_id FROM blocked_channels")}

    def list_blocked_channels(self) -> list[dict]:
        return self._rows("SELECT * FROM blocked_channels ORDER BY channel_title COLLATE NOCASE")

    # ------------------------------------------------------------ avoid words
    def add_avoid_word(self, word: str, source: str = "user", auto: bool = False,
                       blocked: bool = True, protected: bool = False) -> bool:
        word = str(word).strip()
        if not word:
            return False
        try:
            self._write(
                "INSERT INTO avoid_words(word,source,blocked,protected,auto,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (word, source, int(blocked), int(protected), int(auto), self.clock()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_avoid_word(self, word: str, blocked: bool, protected: bool | None = None) -> None:
        if protected is None:
            self._write("UPDATE avoid_words SET blocked=? WHERE word=?", (int(blocked), word))
        else:
            self._write(
                "UPDATE avoid_words SET blocked=?, protected=? WHERE word=?",
                (int(blocked), int(protected), word),
            )

    def remove_avoid_word(self, word: str) -> None:
        self._write("DELETE FROM avoid_words WHERE word=?", (word,))

    def list_avoid_words(self) -> list[dict]:
        rows = self._rows("SELECT * FROM avoid_words ORDER BY word COLLATE NOCASE")
        for row in rows:
            row["blocked"] = bool(row["blocked"])
            row["protected"] = bool(row["protected"])
            row["auto"] = bool(row["auto"])
        return rows

    def blocked_words(self) -> set[str]:
        return {r["word"].lower() for r in self._rows(
            "SELECT word FROM avoid_words WHERE blocked=1")}

    # ---------------------------------------------------------- style filters
    def add_style_feature(self, kind: str, pattern: str = "",
                          source: str = "user", enabled: bool = True,
                          auto: bool = False) -> int | None:
        if kind not in ("caps", "emoji", "regex"):
            raise ValueError("kind must be caps, emoji or regex")
        if kind == "regex":
            if not pattern:
                return None
            import re
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}")
        try:
            return self._write(
                "INSERT INTO style_features(kind,pattern,source,enabled,auto,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (kind, pattern, source, int(enabled), int(auto), self.clock()),
            )
        except sqlite3.IntegrityError:
            return None

    def set_style_feature(self, feature_id: int, enabled: bool,
                          protected: bool | None = None) -> None:
        if protected is None:
            self._write("UPDATE style_features SET enabled=? WHERE id=?",
                        (int(enabled), feature_id))
        else:
            self._write(
                "UPDATE style_features SET enabled=?, protected=? WHERE id=?",
                (int(enabled), int(protected), feature_id),
            )

    def remove_style_feature(self, feature_id: int) -> None:
        self._write("DELETE FROM style_features WHERE id=?", (feature_id,))

    def list_style_features(self) -> list[dict]:
        rows = self._rows("SELECT * FROM style_features ORDER BY kind, pattern")
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["protected"] = bool(row["protected"])
            row["auto"] = bool(row["auto"])
        return rows

    # ---------------------------------------------------------------- videos
    def upsert_video(self, video: dict) -> None:
        defaults = {
            "title": "", "channel_id": "", "channel_title": "",
            "published_at": "", "published_ts": 0, "tags": [],
            "topic_text": "", "lane": "", "modifier": "",
        }
        row = {col: video.get(col) for col in VIDEO_COLUMNS}
        for key, fallback in defaults.items():
            if row.get(key) is None:
                row[key] = fallback
        row["tags"] = json.dumps(row.get("tags") or [], ensure_ascii=False)
        row["first_seen"] = video.get("first_seen") or self.clock()
        cols = ",".join(VIDEO_COLUMNS)
        marks = ",".join("?" for _ in VIDEO_COLUMNS)
        updates = ",".join(f"{c}=excluded.{c}" for c in VIDEO_COLUMNS
                           if c != "video_id")
        self._write(
            f"INSERT INTO videos({cols}) VALUES({marks}) "
            f"ON CONFLICT(video_id) DO UPDATE SET {updates}",
            tuple(row[c] for c in VIDEO_COLUMNS),
        )

    def get_video(self, video_id: str) -> dict | None:
        row = self._one("SELECT * FROM videos WHERE video_id=?", (video_id,))
        if row:
            row["tags"] = _loads_list(row["tags"])
        return row

    def list_videos(self) -> list[dict]:
        rows = self._rows("SELECT * FROM videos")
        for row in rows:
            row["tags"] = _loads_list(row["tags"])
        return rows

    # ------------------------------------------------------------------ seen
    def mark_seen(self, video_id: str, ts: float | None = None) -> None:
        self._write(
            "INSERT OR IGNORE INTO seen(video_id,ts) VALUES(?,?)",
            (video_id, ts if ts is not None else self.clock()),
        )

    def is_seen(self, video_id: str) -> bool:
        return self._one("SELECT 1 FROM seen WHERE video_id=?", (video_id,)) is not None

    def seen_ids(self) -> set[str]:
        return {r["video_id"] for r in self._rows("SELECT video_id FROM seen")}

    def seen_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM seen")
        return row["n"] if row else 0

    def clear_history(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM seen")
            self.conn.execute("DELETE FROM pool")
            self.conn.execute("DELETE FROM events")
            self.conn.execute("DELETE FROM searches")
            self.conn.execute("UPDATE topics SET searches=0, plays=0, rejects=0")
            self.conn.execute("UPDATE modifiers SET searches=0, plays=0")
            self.conn.execute(
                "UPDATE trial_topics SET searches_used=0, impressions=0, clicks=0"
            )
            self.conn.commit()

    # ---------------------------------------------------------------- events
    def add_event(self, video_id: str | None, topic_text: str, channel_id: str,
                  kind: str, weight: float, ts: float | None = None,
                  meta: str = "", dedupe: bool = False) -> int | None:
        if dedupe and video_id:
            existing = self._one(
                "SELECT 1 FROM events WHERE video_id=? AND kind=?", (video_id, kind)
            )
            if existing:
                return None
        return self._write(
            "INSERT INTO events(ts,video_id,topic_text,channel_id,kind,weight,meta) "
            "VALUES(?,?,?,?,?,?,?)",
            (ts if ts is not None else self.clock(), video_id, topic_text,
             channel_id, kind, weight, meta),
        )

    def list_events(self) -> list[dict]:
        return self._rows("SELECT * FROM events ORDER BY ts")

    def event_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM events")
        return row["n"] if row else 0

    # ------------------------------------------------------------------ pool
    def add_pool(self, video: dict, topic_id: int | None, topic_text: str,
                 lane: str, modifier: str, now: float, expiry_days: float) -> None:
        payload = dict(video)
        self._write(
            "INSERT INTO pool(video_id,added_ts,expires_ts,topic_text,topic_id,lane,"
            "modifier,payload) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(video_id) DO NOTHING",
            (video["video_id"], now, now + expiry_days * 86400, topic_text,
             topic_id, lane, modifier, json.dumps(payload, ensure_ascii=False)),
        )

    def pool_entries(self, now: float) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM pool WHERE expires_ts>=? ORDER BY added_ts", (now,)
        )
        for row in rows:
            row["video"] = json.loads(row["payload"])
        return rows

    def pool_ids(self) -> set[str]:
        return {r["video_id"] for r in self._rows("SELECT video_id FROM pool")}

    def drop_pool(self, video_id: str) -> None:
        self._write("DELETE FROM pool WHERE video_id=?", (video_id,))

    def purge_pool(self, now: float) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM pool WHERE expires_ts<?", (now,))
            self.conn.commit()
            return cur.rowcount

    def pool_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM pool")
        return row["n"] if row else 0

    # ---------------------------------------------------------------- trials
    def add_trial(self, topic_id: int, text: str, budget: int,
                  parent_video: str, now: float) -> int:
        return self._write(
            "INSERT INTO trial_topics(topic_id,text,created_ts,parent_video,budget) "
            "VALUES(?,?,?,?,?)",
            (topic_id, text, now, parent_video, budget),
        )

    def list_trials(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trial_topics"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY created_ts"
        return self._rows(sql, params)

    def get_trial_for_topic(self, topic_id: int) -> dict | None:
        return self._one("SELECT * FROM trial_topics WHERE topic_id=?", (topic_id,))

    def update_trial(self, trial_id: int, **fields) -> None:
        allowed = {"status", "searches_used", "budget", "impressions", "clicks"}
        for key in fields:
            if key not in allowed:
                raise ValueError(key)
        assignments = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE trial_topics SET {assignments} WHERE id=?",
                    (*fields.values(), trial_id))

    def bump_trial(self, topic_id: int, field: str, amount: int = 1) -> None:
        if field not in ("searches_used", "impressions", "clicks"):
            raise ValueError(field)
        self._write(
            f"UPDATE trial_topics SET {field}={field}+? WHERE topic_id=?",
            (amount, topic_id),
        )

    # --------------------------------------------------------------- searches
    def record_search(self, now: float, topic_text: str, topic_id: int | None,
                      lane: str, modifier: str, order_by: str,
                      published_after: str, published_before: str,
                      units: int, results: int, new_candidates: int,
                      mode: str) -> None:
        self._write(
            "INSERT INTO searches(ts,topic_text,topic_id,lane,modifier,order_by,"
            "published_after,published_before,units,results,new_candidates,mode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, topic_text, topic_id, lane, modifier, order_by,
             published_after, published_before, units, results, new_candidates, mode),
        )

    def recent_searches(self, limit: int) -> list[dict]:
        return self._rows("SELECT * FROM searches ORDER BY ts DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ quota
    def quota_row(self, day: str) -> dict:
        row = self._one("SELECT * FROM quota WHERE day=?", (day,))
        return row or {"day": day, "units": 0, "search_calls": 0, "videos_calls": 0}

    def add_quota(self, day: str, units: int = 0, search_calls: int = 0,
                  videos_calls: int = 0) -> None:
        self._write(
            "INSERT INTO quota(day,units,search_calls,videos_calls) VALUES(?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET units=units+excluded.units, "
            "search_calls=search_calls+excluded.search_calls, "
            "videos_calls=videos_calls+excluded.videos_calls",
            (day, units, search_calls, videos_calls),
        )

    # ------------------------------------------------------------------ misc
    def counts(self) -> dict:
        out = {}
        for table in ("topics", "modifiers", "blocked_channels", "avoid_words",
                      "style_features", "videos", "seen", "events", "pool",
                      "trial_topics", "searches", "dayparts"):
            row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = row["n"] if row else 0
        return out

    # ------------------------------------------------------- seed / transfer
    def apply_seed(self, seed: dict, source: str = "seed") -> None:
        if not isinstance(seed, dict):
            return
        topics = seed.get("topics") or {}
        if isinstance(topics, dict):
            for lane in ("core", "neighbour", "wildcard"):
                for entry in topics.get(lane) or []:
                    if isinstance(entry, dict):
                        text = str(entry.get("text") or "")
                        intent = str(entry.get("intent") or "learning")
                    else:
                        text, intent = str(entry), "learning"
                    if not text.strip():
                        continue
                    topic_id = self.add_topic(text, lane, source=source,
                                              intent=intent)
                    if topic_id is None and source == "seed":
                        # Topic exists. Keep the user's own edits, but let the
                        # seed declare intent for topics that came from a seed.
                        existing = self._one(
                            "SELECT id,source,intent FROM topics "
                            "WHERE text=? AND lane=?", (text.strip(), lane))
                        if (existing and existing["source"] == "seed"
                                and existing["intent"] != intent.lower()):
                            self.set_topic_intent(existing["id"], intent)
        for text in seed.get("query_modifiers") or []:
            if str(text).strip():
                self.add_modifier(str(text), source=source)
        for word in seed.get("avoid_words") or []:
            if str(word).strip():
                self.add_avoid_word(str(word), source=source)
        for channel in seed.get("blocked_channels") or []:
            if isinstance(channel, dict) and channel.get("channel_id"):
                self.block_channel(str(channel["channel_id"]),
                                   str(channel.get("channel_title", "")), source)
        for feature in seed.get("style_features") or []:
            if isinstance(feature, dict) and feature.get("kind"):
                try:
                    self.add_style_feature(
                        str(feature["kind"]), str(feature.get("pattern", "")),
                        source=source)
                except ValueError:
                    pass
        for profile in seed.get("dayparts") or []:
            if not isinstance(profile, dict):
                continue
            name = str(profile.get("name") or "").strip()
            if not name:
                continue
            existing = {p["name"] for p in self.list_dayparts()}
            if name in existing:
                continue
            try:
                self.add_daypart(profile, source=source)
            except (ValueError, TypeError):
                pass
        for key, value in (seed.get("settings") or {}).items():
            try:
                self.set_setting(key, value)
            except ValueError:
                pass

    def export_all(self) -> dict:
        data = {"version": config.SCHEMA_VERSION, "tables": {}}
        for table in TABLES:
            rows = self._rows(f"SELECT * FROM {table}")
            if table == "settings":
                rows = [{"key": r["key"], "value": r["value"]} for r in rows]
            data["tables"][table] = rows
        data["meta"] = {"exported_at": self.clock(), "counts": self.counts()}
        return data

    def import_all(self, data: dict) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
            raise ValueError("import file is not a yt-discover export")
        tables = data["tables"]
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            try:
                if self.conn.in_transaction:
                    self.conn.commit()
                self.conn.execute("BEGIN")
                for table in TABLES:
                    self.conn.execute(f"DELETE FROM {table}")
                for table in TABLES:
                    rows = tables.get(table) or []
                    if not isinstance(rows, list):
                        raise ValueError(f"bad table {table}")
                    allowed = {
                        r["name"] for r in self._rows(
                            f"PRAGMA table_info({table})")
                    }
                    for row in rows:
                        if not isinstance(row, dict) or not row:
                            continue
                        cols = [c for c in row.keys() if c in allowed]
                        if not cols:
                            continue
                        marks = ",".join("?" for _ in cols)
                        self.conn.execute(
                            f"INSERT INTO {table}({','.join(cols)}) VALUES({marks})",
                            tuple(row[c] for c in cols),
                        )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self.conn.execute("PRAGMA foreign_keys=ON")


_MISSING = object()


def _loads_list(raw) -> list:
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []
