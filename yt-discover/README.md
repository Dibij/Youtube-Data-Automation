# yt-discover

A personal YouTube discovery feed that runs on `127.0.0.1` only. It searches
YouTube through the official Data API v3, remembers every video it has shown
you, and learns from your clicks. A small watcher starts it when Brave starts,
opens it in a Brave tab, and stops it when Brave closes.

Phase 1 is complete. Phase 2 (Ollama topic suggestions) is not built yet.

## Requirements

- Linux (tested on Ubuntu + GNOME), Python 3.10 or newer
- Brave installed from the `.deb` package (binary under `/opt/brave.com/brave/`)
- A YouTube Data API v3 key (see below)
- No pip packages. Standard library only.

## 1. Get a YouTube Data API v3 key

1. Open <https://console.cloud.google.com/> and create (or pick) a project.
2. Go to **APIs & Services → Library**, search for **YouTube Data API v3**,
   open it and click **Enable**.
3. Go to **APIs & Services → Credentials → Create credentials → API key**.
   Copy the key.
4. Optional but recommended: click the key, and under **API restrictions**
   choose **Restrict key → YouTube Data API v3**. Application restrictions are
   not needed for a server-side key; do not expose it to a browser.

**Billing / credit card:** the API has a free daily quota, and in my experience
a billing account is *not* required to create a key and use the free quota.
Google's signup flow has changed over time and may still ask you to add a
billing account or verify a card for some projects or regions. I could not
verify your specific account state, so treat this as: **try without billing
first; if the console will not let you enable the API, add a billing account
and set a budget alert.** I am not certain either way for every account.

**Quota facts I verified on 2026-11-14** against
<https://developers.google.com/youtube/v3/determine_quota_cost>:

- Default allocation: **10,000 units/day** for most endpoints, plus a separate
  **100 calls/day** bucket for `search.list` and for `videos.insert`.
- `videos.list` costs **1 unit** per call (verify in the table on that page).
- `search.list` is documented today as **1 unit per call in its own 100/day
  bucket**. Older documentation and countless tutorials say **100 units per
  call**. This app tracks *both* rails so the numbers are safe under either
  model; see `search_cost_units` and `search_calls_daily_limit` in settings.
- Daily quotas reset at **midnight Pacific Time (PT)**. The app computes the
  Pacific day with `zoneinfo` (`America/Los_Angeles`).

**Removed API:** `search.list`'s `relatedToVideoId` parameter was deprecated on
2023-06-12 and **removed on 2023-08-07** (YouTube Data API revision history).
This app never uses it. Neighbour discovery is done from `snippet.tags` and
title keywords instead.

## 2. Where the key goes

Put it in `~/.config/yt-discover/env` (created by `install.sh`, chmod 600):

```
YOUTUBE_API_KEY=AIza...
```

or export `YOUTUBE_API_KEY` in the environment. The key is read server-side
only. It is never written to SQLite, never sent to the browser, never logged:
all outward errors pass through `config.sanitize_error()`, which strips URLs,
quoted fragments and `AIza...` patterns.

## 3. Run it by hand

```bash
cd yt-discover
cp seed.example.json ~/.config/yt-discover/seed.json      # optional but wanted
YOUTUBE_API_KEY=AIza... python3 -m yt_discover
# open http://127.0.0.1:8765/
```

Useful flags/env:

```bash
python3 -m yt_discover --port 9000 --no-browser
python3 -m yt_discover --check        # print resolved paths and whether a key was found
YT_DISCOVER_DATA_DIR=/tmp/ytd python3 -m yt_discover
```

The database is at `~/.local/share/yt-discover/discover.db`. Delete it to start
over completely, or use **Reset history** in the UI to clear seen videos,
events, the candidate pool and counters while keeping topics and settings.

## 4. Install the watcher (start/stop with Brave)

```bash
cd yt-discover
./install.sh --seed        # copies app, creates env file, installs+enables the user unit
```

Then edit `~/.config/yt-discover/env` and put your real key in it.

What the watcher does every 2 seconds:

- scans `/proc` for the **main** Brave process: executable
  `/opt/brave.com/brave/brave` (basename configurable) with no `--type=`
  argument. Renderer, GPU, zygote, utility and crashpad processes are ignored.
- Brave appears → takes over a stale `yt-discover` server on the port (a
  leftover manual run is killed only if its cmdline contains `yt_discover`;
  unrelated programs are never touched), starts `python3 -m yt_discover` in its
  own session/process group, waits up to 20 s for `/api/status` **with the
  instance token it generated**, then opens `http://127.0.0.1:8765/` once.
- Readiness must come from the child it just spawned. If that child exits
  (`Address already in use`, missing module, etc.) the watcher logs its stderr,
  retries with backoff, and after 3 failures pauses for 60 s instead of
  logging forever.
- server crashes → restarted, at most 3 times per 60 s.
- Brave gone → after `grace_seconds` (15 s) the server gets SIGTERM, then
  SIGKILL after 5 s; watcher returns to STOPPED.
- A **new Brave session** (new main pid) inside the grace window keeps the
  server and opens one tab for that new session. A transient scan miss with the
  same pid opens nothing. The last tab pid is persisted in
  `~/.local/state/yt-discover/watcher-state.json`, so restarting the watcher
  does not re-open a tab for a Brave session that already has one.

Config is data: `~/.config/yt-discover/watcher.json` (port, grace seconds,
Brave prefix, profile directory, open_tab) or `YT_DISCOVER_*` env vars.

### What to check on your machine

I verified on this machine:

- `/usr/bin/brave-browser` → `/opt/brave.com/brave/brave-browser`, which execs
  `/opt/brave.com/brave/brave`
- the main process reports its exe as `/opt/brave.com/brave/brave`
- a running Brave had `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`,
  `XDG_RUNTIME_DIR=/run/user/1000`, `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`

What I could **not** verify (do this yourself):

```bash
ps -eo pid,args | grep -E '[b]rave' | head
readlink /proc/$(pgrep -f '/opt/brave.com/brave/brave$' | head -1)/exe
```

- Confirm the main process is `brave-browser` (shell wrapper) *and* a child
  `/opt/brave.com/brave/brave` without `--type=`. The watcher matches any
  process whose exe is under the prefix and lacks `--type=`, so both the
  wrapper-spawned binary and a direct launch are handled.
- If you use a Flatpak/Snap Brave, change `brave_exe_prefix` in
  `watcher.json` (and `brave_command` if the CLI wrapper differs).
- If the tab does not open when started by systemd, check
  `journalctl --user -u yt-discover-watcher -n 50`; the watcher copies display
  vars from the running Brave process's `/proc/<pid>/environ`. I verified the
  reading code, not the actual systemd launch on your desktop.

### If the watcher logs `No module named yt_discover`

The installed `watcher.py` starts the server from the directory that contains
`yt_discover/` (its own directory), which is correct for both the repo and
`~/.local/share/yt-discover`. An older build used the parent directory, so
every child exited with code 1 while a leftover manual server made the logs
look healthy. Re-run `./install.sh`.

### Brave setting that matters

In **Brave Settings → System**, turn **OFF** "Continue running background apps
when Brave is closed". Otherwise closing the window leaves the Brave process
alive, the watcher still sees Brave, and the feed keeps running.

### Uninstall

```bash
cd yt-discover
./uninstall.sh            # removes service + app files, keeps db/config/logs
./uninstall.sh --purge    # also deletes config, database and logs
```

## 5. Using the UI

- **Discover** (`n`) builds a batch. The engine serves from the candidate pool
  first (0 quota), then searches YouTube only as needed.
- **Surprise me** runs a wildcard-lane-only batch.
- Click a thumbnail/title → embedded `youtube-nocookie.com` player at the top
  (autoplay). If a video refuses to embed, use **Open on YouTube**.
- Card buttons: More like this, Not this, Block channel, Enough of this topic.
  Every shown video is marked seen immediately, so reloading gives new ones.
- **Tune** opens the panel: day parts, topics table (with intent), lane
  sliders, blocked words with evidence, title style filters, blocked channels,
  trial/retired topics, quota meter, settings, export/import and reset.
- **Reload seed.json** (Tune) re-applies the seed *additively*: it adds new
  topics/modifiers/words/day parts and updates intents on seed-sourced topics,
  but never deletes or overwrites your own edits. Safe to press repeatedly.
- **Explore** slider shifts weight from core to neighbour/wildcard. A hard
  exploration floor (default 25% non-core over a rolling window) and a
  per-topic ceiling (default 30% of a batch's searches) stop new ruts.

### Day parts: learning by day, fun at night

Every topic carries a free-text **intent** tag (`learning`, `fun`, or anything
you invent). A **day part** is a local-time window that picks lane shares and
which intents may appear. Defaults:

| Window | Hours (local) | Core / Neighbour / Wildcard | Visible intents |
|---|---|---|---|
| Learning day | 06:00-19:00 | 55 / 30 / 15 | learning |
| Evening fun | 19:00-23:00 | 45 / 25 / 30 | fun |
| Late night | 23:00-06:00 | 35 / 25 / 40 | fun |

At **19:00 exactly** the switch happens (windows are half-open, so 18:59 is
still Learning day). Edit everything in **Tune -> Day parts**: name, hours,
lane percentages, and `intent:weight` pairs (weight `0` hides that intent in
that window). Add or remove windows, or toggle the feature off with the
**Enabled** checkbox. The panel shows the active window and the lane shares
actually in use.

Day parts never break the feed: if a window hides every topic, that filter is
ignored for the batch and the UI says so. The exploration floor, per-topic
ceiling and blocked-word/channel filters still apply.

Set intents on new topics with the learning/fun dropdown next to the quick-add
box, or per row in the topics table. In `seed.json` a topic may be a plain
string (intent `learning`) or `{"text": "...", "intent": "fun"}`.

### Extensions (Unhook and Volume Master)

On this machine both are installed:

- `khncfooichmfjbepaaaebmommgaepoid` **Unhook** 1.6.9
- `jghecgabfgfdldnmbfkhmffcabddioke` **Volume Master** 2.4.0 ("up to 600%")

They only act on `youtube.com` pages. They do **not** run inside the embedded
player (`youtube-nocookie.com` iframe), so the embed still shows YouTube's
recommendations/end cards and Volume Master cannot change its volume.

Volume Master is installed but **not pinned** to the toolbar (Brave profile
`pinned_extensions` was empty), which is why you could not find it. To reach it:

1. Click the puzzle-piece **Extensions** icon at the right end of the toolbar.
2. Find **Volume Master** and click the **pin** icon next to it. The round
   gauge icon then appears in the toolbar; click it to set the boost.
3. It will show "not available on this page" for the embedded player. Use
   **Open on YouTube** on a card, then adjust the volume there.

To keep Unhook active for videos opened that way, it must also be allowed on
`youtube.com` (it already is by default).

## 6. Back up your learned data

Everything learned lives in SQLite plus config:

```bash
mkdir -p ~/ytd-backup
sqlite3 ~/.local/share/yt-discover/discover.db ".backup '$HOME/ytd-backup/discover.db'"
cp -a ~/.config/yt-discover ~/ytd-backup/config
```

Or use **Export state** in the Tune panel (JSON of all tables) and **Import
state** to restore. Export/import round-trips (covered by a test). The API key
env file is plain text; treat that backup as secret.

## 7. Architecture

```
yt_discover/
  config.py     defaults + paths + API key loading + error sanitizing
  db.py         SQLite schema, pool, events, settings, seed import, export/import
  youtube.py    official Data API client (search.list, videos.list) + quota day
  learning.py   decay, Thompson sampling, lane planner, word/style/trial learning
  discovery.py  batch assembly, filters, pool-first, quota rails
  server.py     127.0.0.1 HTTP server, Host/Content-Type/CSP checks, JSON API
  __main__.py   CLI entry point
  static/       index.html, app.js, style.css (no innerHTML, textContent only)
watcher.py      Brave detection + start/stop state machine (stdlib only)
```

### How topic selection works (two sentences)

Each search first picks a lane (core / neighbour / wildcard) from the share
mix, then picks a topic inside that lane by Beta-Bernoulli Thompson sampling
where the Beta parameters are `1 + decayed positive evidence` and
`1 + decayed negative evidence`, so well-liked topics win often while every
topic keeps a nonzero chance. A small uniform-exploration probability plus the
rolling exploration floor keep unloved topics from being starved forever, and
a per-topic search cap within each batch enforces the ceiling.

### YouTube search construction

Per search: `q = topic` optionally plus a learned modifier, random
`publishedAfter`/`publishedBefore` (sometimes none, sometimes a
60–365-day window somewhere in the last N years, or last 12 months),
random `order` from `relevance|date|viewCount|rating`, `type=video`,
`videoEmbeddable=true`, `maxResults=50`, `safeSearch` and
`relevanceLanguage` from settings. Then one `videos.list` call
(`snippet,contentDetails,statistics`) for the unseen ids.

## 8. Tests

```bash
cd yt-discover
python3 -m unittest discover -s tests -v
```

71 tests, no network: fake YouTube API, fake clock, fake process list, a real
server subprocess smoke test, Host/Content-Type/CSP/key-leak checks, filter,
pool, quota, decay, bandit, floor/ceiling, word/style/trial learning,
persistence and export/import. Expected tail: `Ran 71 tests ... OK`.

## 9. DECISIONS and ASSUMPTIONS

See the end of the delivery message for the full list; the important ones are
also reflected in `yt_discover/config.py` (single source of numeric defaults)
and `seed.example.json` (all textual data).

### TODO for you to verify (unverified API details)

- `search.list` quota: old "100 units per call vs 10,000/day" model vs current
  "1 unit, separate 100 calls/day bucket". Both are configurable and tracked.
- Whether your Google Cloud project needs a billing account for the free quota.
- Any future change to `videoEmbeddable` / `publishedAfter` semantics: check
  <https://developers.google.com/youtube/v3/docs/search/list>.

## 10. Final checklist

- [ ] `python3 -m unittest discover -s tests` passes (71 tests)
- [ ] `python3 -m yt_discover --check` shows the expected paths and key found
- [ ] run by hand, open `http://127.0.0.1:8765/`, press `n`, click a card,
      click More like this / Not this, check quota moves
- [ ] Tune → Day parts shows the window matching your clock; before 19:00 the
      batch is learning topics, after 19:00 it is fun topics
- [ ] `./install.sh --seed`, put the key in `~/.config/yt-discover/env`
- [ ] `systemctl --user status yt-discover-watcher`
- [ ] start Brave → tab opens exactly once
- [ ] restart Brave → one new tab for the new session (server kept)
- [ ] `systemctl --user restart yt-discover-watcher` while Brave is open →
      no extra tab
- [ ] restart Brave quickly → still one tab, server kept
- [ ] close Brave → after ~15 s the server is gone
      (`curl http://127.0.0.1:8765/api/status` fails)
- [ ] turn off Brave's "continue background apps" setting
