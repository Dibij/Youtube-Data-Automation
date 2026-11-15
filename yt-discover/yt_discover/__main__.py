"""Entry point: python -m yt_discover [--port N] [--no-browser] [--api-key ...]

Environment overrides:
  YT_DISCOVER_DATA_DIR, YT_DISCOVER_CONFIG_DIR, YT_DISCOVER_STATE_DIR,
  YT_DISCOVER_SEED, YT_DISCOVER_LOG, YOUTUBE_API_KEY
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config
from .db import Database
from .server import create_server
from .youtube import YouTubeAPI

log = logging.getLogger("yt-discover")


def setup_logging(log_path: Path | None) -> None:
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    log.addHandler(stream)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        log.addHandler(handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="yt-discover")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("YT_DISCOVER_PORT", 0)) or None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="print resolved paths and exit")
    args = parser.parse_args(argv)

    data_dir = config.data_dir()
    log_path = Path(os.environ.get("YT_DISCOVER_LOG",
                                   config.state_dir() / "server.log"))
    setup_logging(log_path)

    if args.check:
        print(json.dumps({
            "data_dir": str(data_dir),
            "config_dir": str(config.config_dir()),
            "db": str(config.db_path()),
            "env_file": str(config.env_file_path()),
            "seed": str(config.seed_path()) if config.seed_path() else None,
            "has_api_key": bool(config.load_api_key()),
        }, indent=2))
        return 0

    db = Database(config.db_path())
    api_key = config.load_api_key()
    api = YouTubeAPI(api_key)
    port = args.port or int(db.get_setting("server_port", 8765))
    try:
        server = create_server(db, api, port=port, host=args.host,
                               log_path=log_path)
    except OSError as exc:
        log.error("cannot bind %s:%s: %s", args.host, port, exc)
        print(f"error: cannot bind {args.host}:{port}: {exc}", file=sys.stderr)
        return 1
    actual = server.actual_port
    log.info("listening on http://127.0.0.1:%s (db=%s, key=%s)",
             actual, config.db_path(), "yes" if api_key else "no")

    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("signal %s received; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if not args.no_browser and os.environ.get("YT_DISCOVER_NO_OPEN") != "1":
        try:
            webbrowser.open(f"http://127.0.0.1:{actual}/", new=2)
        except Exception:
            pass

    print(f"yt-discover running at http://127.0.0.1:{actual}/", flush=True)
    try:
        while not stop.wait(0.5):
            pass
    finally:
        server.shutdown()
        server.server_close()
        db.close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
