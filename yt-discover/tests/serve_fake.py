#!/usr/bin/env python3
"""Launch the real yt-discover HTTP server in a subprocess with the fake API.

Used by tests/test_smoke.py. Prints the bound port on stdout, then serves
until SIGTERM.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fake_youtube import FakeYouTube, make_video  # noqa: E402
from yt_discover import config  # noqa: E402
from yt_discover.db import Database  # noqa: E402
from yt_discover.server import create_server  # noqa: E402


def main() -> int:
    videos = [
        make_video(f"smk{i:08d}", title=f"smoke video {i}",
                   channel_id=f"UCsmoke{i:05d}", tags=["smoke", "test"])
        for i in range(20)
    ]
    api = FakeYouTube({"": videos})
    db = Database(config.db_path())
    server = create_server(db, api, port=0)
    print(server.actual_port, flush=True)

    def stop(_signum, _frame):
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
