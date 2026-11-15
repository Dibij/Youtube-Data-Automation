"""Shared test helpers: temp dirs, fake clock, seeded database."""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yt_discover import config  # noqa: E402
from yt_discover.db import Database  # noqa: E402
from yt_discover.discovery import DiscoveryEngine  # noqa: E402

TEST_SEED = {
    "topics": {
        "core": ["core alpha", "core beta"],
        "neighbour": ["neighbour alpha", "neighbour beta"],
        "wildcard": ["wild alpha", "wild beta"],
    },
    "query_modifiers": ["explained", "deep dive"],
    "avoid_words": ["summoner"],
}


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def days(self, days: float) -> None:
        self.now += days * 86400


class TempProject(unittest.TestCase):
    """Base class giving each test isolated data/config/state directories."""

    seed = TEST_SEED

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytd-test-"))
        self.data_dir = self.tmp / "data"
        self.config_dir = self.tmp / "config"
        self.state_dir = self.tmp / "state"
        self.config_dir.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.seed_file = self.config_dir / "seed.json"
        self.seed_file.write_text(json.dumps(self.seed), encoding="utf-8")
        self._old_env = {}
        for key, value in {
            "YT_DISCOVER_DATA_DIR": str(self.data_dir),
            "YT_DISCOVER_CONFIG_DIR": str(self.config_dir),
            "YT_DISCOVER_STATE_DIR": str(self.state_dir),
            "YT_DISCOVER_SEED": str(self.seed_file),
        }.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.clock = FakeClock()
        self.db = Database(self.data_dir / "discover.db", clock=self.clock)
        self.rng = random.Random(1234)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, api):
        return DiscoveryEngine(self.db, api, clock=self.clock, rng=self.rng)
