"""Localhost HTTP server for yt-discover (stdlib only).

Security posture:
  * binds 127.0.0.1 only
  * Host header must be localhost:PORT or 127.0.0.1:PORT (DNS-rebinding guard)
  * POST requires Content-Type: application/json
  * no CORS headers at all
  * strict CSP; iframe embedding limited to youtube-nocookie.com
  * API key stays server-side; errors are sanitized of URLs and key material
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import config
from . import learning
from . import youtube as yt_api
from .discovery import DiscoveryEngine

STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 1_000_000

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src https://i.ytimg.com data:; "
    "frame-src https://www.youtube-nocookie.com; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)

ALLOWED_KINDS = ("play", "more_like_this", "not_this", "enough_of_this_topic",
                 "block_channel")


class AppState:
    """Shared server state: one DB, one engine, one API client."""

    def __init__(self, db, api, clock=None, rng=None, instance_id=None):
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        if rng is not None:
            kwargs["rng"] = rng
        self.db = db
        self.api = api
        self.engine = DiscoveryEngine(db, api, **kwargs)
        self.instance_id = (instance_id
                            or os.environ.get("YT_DISCOVER_INSTANCE_ID")
                            or secrets.token_hex(8))
        self.pid = os.getpid()

    def fetch(self, count=None, mode="normal") -> dict:
        payload = self.engine.fetch_batch(count=count, mode=mode)
        if isinstance(payload.get("messages"), list):
            payload["messages"] = [
                config.sanitize_error(str(message), self.api.api_key)
                for message in payload["messages"]
            ]
        if payload.get("filters_warning"):
            payload["filters_warning"] = config.sanitize_error(
                str(payload["filters_warning"]), self.api.api_key)
        return payload

    def snapshot(self) -> dict:
        db = self.db
        settings = db.get_settings()
        topics = db.list_topics()
        now = self.engine.now()
        half_life = float(settings.get("half_life_days", 14))
        events = db.list_events()
        scored = learning.topic_scores(events, now, half_life)
        mods_scored = learning.modifier_scores(events, now, half_life)
        topics_out = []
        for topic in topics:
            entry = scored.get(topic["text"]) or {}
            topics_out.append({
                **topic,
                "score": round(entry.get("score", 0.0), 3),
                "pos": round(entry.get("pos", 0.0), 3),
                "neg": round(entry.get("neg", 0.0), 3),
            })
        modifiers_out = []
        for modifier in db.list_modifiers():
            entry = mods_scored.get(modifier["text"]) or {}
            modifiers_out.append({**modifier,
                                  "score": round(entry.get("score", 0.0), 3)})
        videos = {v["video_id"]: v for v in db.list_videos()}
        word_ev = learning.word_evidence(
            events, videos, settings.get("stopwords"), now, half_life)
        words = []
        for row in db.list_avoid_words():
            entry = word_ev.get(row["word"].lower()) or {}
            words.append({
                **row,
                "distinct_negatives": entry.get("distinct_negatives", 0),
                "negative_share": round(entry.get("negative_share", 0.0), 2),
                "pos": round(entry.get("pos", 0.0), 2),
                "neg": round(entry.get("neg", 0.0), 2),
            })
        style_ev = learning.style_evidence(
            events, videos, db.list_style_features(), settings, now, half_life)
        styles = []
        for row in db.list_style_features():
            key = learning.feature_key(row["kind"], row.get("pattern", ""))
            entry = style_ev.get(key) or {}
            styles.append({
                **row,
                "distinct_negatives": entry.get("distinct_negatives", 0),
                "negative_share": round(entry.get("negative_share", 0.0), 2),
            })
        profile = self.engine.current_daypart(settings, now)
        dayparts_out = []
        for item in db.list_dayparts():
            dayparts_out.append({
                **item,
                "active": bool(profile and profile["id"] == item["id"]),
            })
        return {
            "instance": self.instance_id,
            "pid": self.pid,
            "topics": topics_out,
            "dayparts": dayparts_out,
            "active_daypart": profile["name"] if profile else None,
            "local_hour": time.localtime(now).tm_hour,
            "modifiers": modifiers_out,
            "words": words,
            "styles": styles,
            "channels": db.list_blocked_channels(),
            "trials": db.list_trials(),
            "settings": settings,
            "quota": self.engine.quota_status(),
            "counts": db.counts(),
            "has_key": self.api.has_key(),
            "lane_shares": learning.effective_lane_shares(settings),
            "lane_shares_now": learning.effective_lane_shares(settings, profile),
            "intents": db.topic_intents(),
        }


def make_handler(state: AppState, port, log_path: Path | None = None):
    def allowed_hosts():
        actual = port() if callable(port) else port
        return {f"localhost:{actual}", f"127.0.0.1:{actual}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "yt-discover/1.0"
        sys_version = ""

        # -------------------------------------------------------- logging
        def log_message(self, fmt, *args):
            if log_path is None:
                return
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"{self.log_date_time_string()} "
                                 f"{fmt % args}\n")
            except OSError:
                pass

        # ------------------------------------------------------- responses
        def _headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def send_json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def send_error_json(self, message, status=HTTPStatus.BAD_REQUEST,
                            kind="error"):
            safe = config.sanitize_error(message, state.api.api_key)
            self.send_json({"error": safe, "kind": kind}, status)

        # ------------------------------------------------------- host check
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            return host in allowed_hosts()

        def _reject_host(self) -> None:
            self.send_error_json("Forbidden host", HTTPStatus.FORBIDDEN,
                                 kind="host")

        def _read_json(self):
            content_type = (self.headers.get("Content-Type") or "").split(";")[0]
            if content_type.strip().lower() != "application/json":
                self.send_error_json(
                    "POST requests must use Content-Type: application/json",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE, kind="content_type")
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self.send_error_json("Invalid request body size",
                                     HTTPStatus.BAD_REQUEST)
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error_json("Malformed JSON body")
                return None
            if not isinstance(data, dict):
                self.send_error_json("JSON body must be an object")
                return None
            return data

        # ---------------------------------------------------------- routing
        def do_GET(self):
            if not self._host_ok():
                return self._reject_host()
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path in ("/", "/index.html"):
                    return self._serve_static("index.html")
                if path == "/app.js":
                    return self._serve_static("app.js")
                if path == "/style.css":
                    return self._serve_static("style.css")
                if path == "/favicon.svg":
                    return self._serve_static("favicon.svg")
                if path == "/api/status":
                    return self.send_json(state.snapshot())
                if path == "/api/feed":
                    # State-changing (it marks videos seen and spends quota),
                    # so it is POST-only: an application/json POST is not a
                    # CORS-"simple" request, so a cross-site page cannot fire
                    # it without a preflight that this server never allows.
                    return self.send_error_json(
                        "Use POST with Content-Type: application/json",
                        HTTPStatus.METHOD_NOT_ALLOWED)
                if path == "/api/export":
                    return self.send_json(state.db.export_all())
                return self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            except Exception as exc:  # never leak a traceback or the key
                return self.send_error_json(
                    f"Server error: {type(exc).__name__}: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            if not self._host_ok():
                return self._reject_host()
            path = urlparse(self.path).path
            data = self._read_json()
            if data is None:
                return
            try:
                return self._post_route(path, data)
            except ValueError as exc:
                return self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
            except yt_api.QuotaExceeded as exc:
                return self.send_error_json(str(exc), HTTPStatus.TOO_MANY_REQUESTS,
                                            kind="quota")
            except yt_api.YouTubeError as exc:
                status = HTTPStatus.TOO_MANY_REQUESTS if exc.kind == "rate" \
                    else HTTPStatus.BAD_GATEWAY
                return self.send_error_json(str(exc), status, kind=exc.kind)
            except Exception as exc:
                return self.send_error_json(
                    f"Server error: {type(exc).__name__}: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR)

        def _post_route(self, path, data):
            db = state.db
            if path == "/api/feed":
                count = data.get("count")
                try:
                    count = int(count) if count is not None else None
                except (TypeError, ValueError):
                    raise ValueError("count must be an integer")
                mode = data.get("mode") or "normal"
                if mode not in ("normal", "wildcard", "pool_only"):
                    raise ValueError("bad mode")
                return self.send_json(state.fetch(count=count, mode=mode))
            if path == "/api/event":
                kind = data.get("kind")
                if kind not in ALLOWED_KINDS:
                    raise ValueError("unknown event kind")
                video_id = str(data.get("video_id") or "")
                if video_id and not yt_api.valid_video_id(video_id):
                    raise ValueError("invalid video id")
                changed = state.engine.record_event(
                    video_id, kind, str(data.get("topic") or ""),
                    str(data.get("channel_id") or ""),
                    str(data.get("channel_title") or ""))
                return self.send_json({"ok": True, "changed": changed,
                                       "quota": state.engine.quota_status()})
            if path == "/api/topics":
                action = data.get("action")
                if action == "add":
                    lane = str(data.get("lane") or "core")
                    text = str(data.get("text") or "")
                    if lane not in ("core", "neighbour", "wildcard"):
                        raise ValueError("bad lane")
                    if not text.strip():
                        raise ValueError("topic text required")
                    topic_id = db.add_topic(
                        text, lane, source="user",
                        intent=str(data.get("intent") or "learning"))
                    if topic_id is None:
                        raise ValueError("topic already exists in that lane")
                    return self.send_json({"ok": True, "id": topic_id})
                if action == "remove":
                    db.remove_topic(int(data.get("id")))
                    return self.send_json({"ok": True})
                if action == "toggle":
                    db.set_topic_enabled(int(data.get("id")),
                                         bool(data.get("enabled")))
                    return self.send_json({"ok": True})
                if action == "intent":
                    if str(data.get("intent") or "").strip() == "":
                        raise ValueError("intent required")
                    db.set_topic_intent(int(data.get("id")),
                                        str(data.get("intent")))
                    return self.send_json({"ok": True})
                raise ValueError("unknown action")
            if path == "/api/dayparts":
                action = data.get("action")
                if action == "add":
                    profile = data.get("daypart")
                    if not isinstance(profile, dict):
                        raise ValueError("daypart object required")
                    return self.send_json(
                        {"ok": True, "id": db.add_daypart(profile, "user")})
                if action == "update":
                    daypart_id = int(data.get("id"))
                    fields = data.get("fields")
                    if not isinstance(fields, dict):
                        raise ValueError("fields object required")
                    db.update_daypart(daypart_id, **fields)
                    return self.send_json({"ok": True})
                if action == "toggle":
                    db.set_daypart_enabled(int(data.get("id")),
                                           bool(data.get("enabled")))
                    return self.send_json({"ok": True})
                if action == "remove":
                    db.remove_daypart(int(data.get("id")))
                    return self.send_json({"ok": True})
                raise ValueError("unknown action")
            if path == "/api/modifiers":
                action = data.get("action")
                if action == "add":
                    value = db.add_modifier(str(data.get("text") or ""),
                                            source="user")
                    if value is None:
                        raise ValueError("modifier already exists or empty")
                    return self.send_json({"ok": True})
                if action == "remove":
                    db.remove_modifier(int(data.get("id")))
                    return self.send_json({"ok": True})
                raise ValueError("unknown action")
            if path == "/api/words":
                action = data.get("action")
                word = str(data.get("word") or "").strip()
                if not word:
                    raise ValueError("word required")
                if action == "add":
                    db.add_avoid_word(word, source="user")
                elif action == "remove":
                    db.remove_avoid_word(word)
                elif action == "unblock":
                    db.set_avoid_word(word, False, protected=True)
                elif action == "block":
                    db.set_avoid_word(word, True, protected=True)
                else:
                    raise ValueError("unknown action")
                return self.send_json({"ok": True})
            if path == "/api/channels":
                action = data.get("action")
                if action == "unblock":
                    channel_id = str(data.get("channel_id") or "")
                    if not yt_api.valid_channel_id(channel_id):
                        raise ValueError("invalid channel id")
                    db.unblock_channel(channel_id)
                    return self.send_json({"ok": True})
                if action == "block":
                    channel_id = str(data.get("channel_id") or "")
                    if not yt_api.valid_channel_id(channel_id):
                        raise ValueError("invalid channel id")
                    db.block_channel(channel_id, str(data.get("title") or ""))
                    return self.send_json({"ok": True})
                raise ValueError("unknown action")
            if path == "/api/styles":
                action = data.get("action")
                if action == "add":
                    kind = str(data.get("kind") or "")
                    pattern = str(data.get("pattern") or "")
                    if kind not in ("caps", "emoji", "regex"):
                        raise ValueError("bad style kind")
                    feature_id = db.add_style_feature(kind, pattern, "user")
                    if feature_id is None:
                        raise ValueError("duplicate or invalid feature")
                    return self.send_json({"ok": True, "id": feature_id})
                if action == "remove":
                    db.remove_style_feature(int(data.get("id")))
                    return self.send_json({"ok": True})
                if action == "toggle":
                    db.set_style_feature(int(data.get("id")),
                                         bool(data.get("enabled")))
                    return self.send_json({"ok": True})
                if action == "protect":
                    db.set_style_feature(int(data.get("id")), False, protected=True)
                    return self.send_json({"ok": True})
                raise ValueError("unknown action")
            if path == "/api/trials":
                action = data.get("action")
                if action == "revive":
                    ok = learning.revive_trial(db, int(data.get("topic_id")))
                    return self.send_json({"ok": ok})
                if action == "remove":
                    topic_id = int(data.get("topic_id"))
                    db.remove_topic(topic_id)
                    return self.send_json({"ok": True})
                if action == "set_budget":
                    topic_id = int(data.get("topic_id"))
                    trial = db.get_trial_for_topic(topic_id)
                    if not trial:
                        raise ValueError("unknown trial topic")
                    db.update_trial(trial["id"],
                                    budget=int(data.get("budget") or 5))
                    return self.send_json({"ok": True})
                raise ValueError("unknown action")
            if path == "/api/settings":
                values = data.get("settings")
                if not isinstance(values, dict):
                    raise ValueError("settings object required")
                db.set_settings(values)
                return self.send_json({"ok": True,
                                       "settings": db.get_settings()})
            if path == "/api/reset":
                db.clear_history()
                return self.send_json({"ok": True})
            if path == "/api/seed":
                result = db.reload_seed()
                return self.send_json(result)
            if path == "/api/learn":
                return self.send_json(state.engine.learn_now())
            if path == "/api/import":
                state.db.import_all(data.get("data") or {})
                return self.send_json({"ok": True})
            return self.send_error_json("Not found", HTTPStatus.NOT_FOUND)

        # ---------------------------------------------------------- static
        def _serve_static(self, name):
            path = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in path.parents or not path.is_file():
                return self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if name.endswith(".js"):
                content_type = "text/javascript; charset=utf-8"
            elif name.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            elif name.endswith(".html"):
                content_type = "text/html; charset=utf-8"
            elif name.endswith(".svg"):
                content_type = "image/svg+xml"
            body = path.read_bytes()
            self._headers(HTTPStatus.OK, content_type, len(body))
            self.wfile.write(body)

    return Handler


def create_server(db, api, port: int = 0, host: str = "127.0.0.1",
                  log_path: Path | None = None, instance_id=None):
    state = AppState(db, api, instance_id=instance_id)
    port_holder = {"port": port}
    handler = make_handler(state, lambda: port_holder["port"], log_path)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    actual_port = server.server_address[1]
    port_holder["port"] = actual_port
    server.state = state
    server.actual_port = actual_port
    server.instance_id = state.instance_id
    return server


def serve(db, api, port: int, log_path: Path | None = None):
    server = create_server(db, api, port=port, log_path=log_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
