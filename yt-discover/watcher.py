#!/usr/bin/env python3
"""Brave start/stop watcher for yt-discover.

Runs as a systemd --user service. Every poll it scans /proc for the *main*
Brave process (executable under /opt/brave.com/brave/ and no --type= argument,
which excludes renderer/GPU/utility children), then drives a tiny state machine:

    STOPPED --(Brave appears)--> starting server, open tab --> RUNNING
    RUNNING --(server dies)--> restart (rate-limited)
    RUNNING --(Brave gone for grace_seconds)--> stop server --> STOPPED

The tab is opened at most once per Brave session.  Display/session variables
are read from the running Brave process's /proc/<pid>/environ because a
systemd user unit does not necessarily inherit the graphical session env.

Configuration is data: ~/.config/yt-discover/watcher.json and/or env vars.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_PORT = 8765
DISPLAY_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY")

log = logging.getLogger("yt-discover-watcher")


@dataclass
class ProcessInfo:
    pid: int
    exe: str = ""
    args: list[str] = field(default_factory=list)


@dataclass
class WatcherConfig:
    port: int = DEFAULT_PORT
    brave_exe_prefix: str = "/opt/brave.com/brave/"
    brave_exe_name: str = "brave"
    brave_command: str = "brave-browser"
    profile_directory: str = ""
    grace_seconds: float = 15.0
    poll_seconds: float = 2.0
    ready_timeout: float = 20.0
    stop_timeout: float = 5.0
    max_restarts: int = 3
    restart_window: float = 60.0
    start_cooldown: float = 10.0
    max_ready_failures: int = 3
    give_up_backoff: float = 60.0
    host: str = "127.0.0.1"
    open_tab: bool = True
    remember_tab: bool = True
    server_log: str = ""
    state_file: str = ""

    @classmethod
    def load(cls, config_path: Path | None = None) -> "WatcherConfig":
        values = {}
        path = config_path or (Path.home() / ".config" / "yt-discover" /
                               "watcher.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                values.update({k: v for k, v in raw.items()
                               if hasattr(cls, k)})
        except (OSError, ValueError):
            pass
        env_map = {
            "YT_DISCOVER_PORT": ("port", int),
            "YT_DISCOVER_GRACE_SECONDS": ("grace_seconds", float),
            "YT_DISCOVER_POLL_SECONDS": ("poll_seconds", float),
            "YT_DISCOVER_BRAVE_PATTERN": ("brave_exe_prefix", str),
            "YT_DISCOVER_BRAVE_CMD": ("brave_command", str),
            "YT_DISCOVER_PROFILE_DIR": ("profile_directory", str),
            "YT_DISCOVER_OPEN_TAB": ("open_tab", lambda v: v not in ("0", "false", "no")),
        }
        for env_key, (attr, caster) in env_map.items():
            raw = os.environ.get(env_key)
            if raw is None or raw == "":
                continue
            try:
                values[attr] = caster(raw)
            except (TypeError, ValueError):
                log.warning("ignoring invalid %s", env_key)
        return cls(**values)


# ---------------------------------------------------------------- /proc scan
def read_process(pid: int, proc_root: str = "/proc") -> ProcessInfo | None:
    base = Path(proc_root) / str(pid)
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        return None
    try:
        raw = (base / "cmdline").read_bytes()
    except OSError:
        return None
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return ProcessInfo(pid=pid, exe=exe, args=args)


def is_main_brave(proc: ProcessInfo, exe_prefix: str,
                  exe_name: str = "brave") -> bool:
    if not proc.exe.startswith(exe_prefix):
        return False
    # Exclude crashpad handlers and other helper binaries that live in the same
    # directory but are not the browser.
    if os.path.basename(proc.exe) != exe_name:
        return False
    # Chromium child processes are forked with the parent's argv memory and
    # hidepid/kernel quirks can make /proc/<pid>/cmdline look like a single
    # space-joined string, so search for --type= anywhere, not only at the
    # start of an argument.
    if any("--type=" in arg for arg in proc.args):
        return False
    return True


def scan_brave(exe_prefix: str = "/opt/brave.com/brave/",
               proc_root: str = "/proc", exe_name: str = "brave") -> list[ProcessInfo]:
    found = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        proc = read_process(int(entry), proc_root)
        if proc and is_main_brave(proc, exe_prefix, exe_name):
            found.append(proc)
    return found


def _parse_environ(raw: bytes) -> dict:
    env = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            name = key.decode("utf-8")
            text = value.decode("utf-8", "replace")
        except UnicodeDecodeError:
            continue
        if name in DISPLAY_KEYS and text:
            env[name] = text
    return env


def _read_environ(pid: int, proc_root: str = "/proc") -> dict:
    try:
        return _parse_environ((Path(proc_root) / str(pid) / "environ").read_bytes())
    except OSError:
        return {}


def _ancestor_pids(pid: int, proc_root: str = "/proc",
                   limit: int = 6) -> list[int]:
    pids = []
    current = pid
    for _ in range(limit):
        try:
            stat = (Path(proc_root) / str(current) / "stat").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            break
        try:
            fields = stat.rsplit(")", 1)[1].split()
            parent = int(fields[1])
        except (IndexError, ValueError):
            break
        if parent <= 1 or parent == current:
            break
        pids.append(parent)
        current = parent
    return pids


def process_display_env(pid: int, proc_root: str = "/proc") -> dict:
    """Best-effort graphical env for a Brave process.

    Chromium's main process often shows an all-NUL or empty environ under
    hidepid/proc hardening; its launcher wrapper and parents usually retain
    the session variables, so we merge whichever sources we can read.
    """
    env = _read_environ(pid, proc_root)
    for ancestor in _ancestor_pids(pid, proc_root):
        for key, value in _read_environ(ancestor, proc_root).items():
            env.setdefault(key, value)
    return env


# ------------------------------------------------------------ server control
class ServerProcess:
    """Owns the yt-discover server child process group.

    Readiness is tied to a per-spawn instance token, not merely to "something
    answers on the port".  Without that, a leftover manual server (or an
    orphaned child from a previous watcher) made the watcher believe a
    crashed instance had come back while the tab was never opened.
    """

    def __init__(self, config, python: str | None = None,
                 package_root: str | None = None, log_path=None):
        self.config = config
        self.python = python or sys.executable
        # watcher.py sits next to the yt_discover package both in the repo and
        # in the installed layout, so the package root is its own directory.
        self.package_root = package_root or str(Path(__file__).resolve().parent)
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.instance_id: str | None = None
        self.stderr_path = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        from secrets import token_hex
        self.instance_id = token_hex(8)
        env = dict(os.environ)
        env["YT_DISCOVER_PORT"] = str(self.config.port)
        env["YT_DISCOVER_NO_OPEN"] = "1"
        env["YT_DISCOVER_INSTANCE_ID"] = self.instance_id
        stderr_target = subprocess.DEVNULL
        if self.log_path is not None:
            try:
                Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
                self.stderr_path = str(self.log_path)
                stderr_target = open(self.stderr_path, "a", encoding="utf-8")
            except OSError:
                stderr_target = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                [self.python, "-m", "yt_discover", "--port",
                 str(self.config.port), "--no-browser"],
                cwd=self.package_root, env=env, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=stderr_target,
                stderr=stderr_target,
            )
        except OSError as exc:
            log.error("failed to start server: %s", exc)
            self.proc = None
            return False
        finally:
            if hasattr(stderr_target, "close"):
                stderr_target.close()
        log.info("server started (pid %s, instance %s)",
                 self.proc.pid, self.instance_id)
        return True

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self):
        return self.proc.poll() if self.proc is not None else None

    def wait_ready(self, timeout: float) -> bool:
        """Wait until *this* instance answers /api/status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return False
            info = status_info(self.config.port)
            if info and info.get("instance") == self.instance_id:
                return True
            time.sleep(0.25)
        return False

    def stop(self, timeout: float = 5.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except OSError:
            try:
                self.proc.terminate()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                try:
                    self.proc.kill()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        log.info("server stopped")
        self.proc = None

    def last_stderr(self, lines: int = 5) -> str:
        if not self.stderr_path:
            return ""
        try:
            with open(self.stderr_path, "r", encoding="utf-8",
                      errors="replace") as handle:
                return "".join(handle.readlines()[-lines:]).strip()
        except OSError:
            return ""


def _fetch_status(port: int, host: str = "127.0.0.1",
                  timeout: float = 2.0) -> dict | None:
    url = f"http://{host}:{port}/api/status"
    request = urllib.request.Request(url, headers={"Host": f"{host}:{port}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def status_info(port: int, host: str = "127.0.0.1",
                timeout: float = 2.0) -> dict | None:
    return _fetch_status(port, host, timeout)


def status_ok(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    return _fetch_status(port, host, timeout) is not None


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def kill_stale_server(port: int, host: str = "127.0.0.1",
                      timeout: float = 6.0, exclude_pid=None) -> bool:
    """Take over a port held by a yt-discover server we did not spawn.

    Only processes whose cmdline contains "yt_discover" are touched, so an
    unrelated program on the same port is never killed.  `exclude_pid` protects
    the caller's own child (never kill a healthy server of ours).
    """
    info = status_info(port, host)
    if info is None:
        return not port_in_use(port, host)
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    if exclude_pid is not None and pid == exclude_pid:
        return True
    if not _is_our_server(pid):
        log.error("port %s is held by a non-yt-discover process (pid %s); "
                  "not touching it", port, pid)
        return False
    log.warning("taking over stale yt-discover server on port %s (pid %s)",
                port, pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_in_use(port, host):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not port_in_use(port, host):
            return True
        time.sleep(0.2)
    return not port_in_use(port, host)


def _is_our_server(pid: int, proc_root: str = "/proc") -> bool:
    try:
        raw = (Path(proc_root) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    joined = " ".join(args)
    return "yt_discover" in joined or "yt-discover" in joined


def status_ok(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    url = f"http://{host}:{port}/api/status"
    request = urllib.request.Request(url, headers={"Host": f"{host}:{port}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def open_tab(config: WatcherConfig, env: dict) -> bool:
    """Hand the URL to the already-running Brave instance."""
    url = f"http://127.0.0.1:{config.port}/"
    command = [config.brave_command]
    if config.profile_directory:
        command.append(f"--profile-directory={config.profile_directory}")
    command.append(url)
    merged_env = dict(os.environ)
    merged_env.update({k: v for k, v in env.items() if v})
    try:
        subprocess.Popen(command, env=merged_env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.error("failed to open Brave tab: %s", exc)
        return False
    log.info("opened app tab in Brave")
    return True


# --------------------------------------------------------------- state machine
STOPPED = "STOPPED"
RUNNING = "RUNNING"


class Watcher:
    """Testable core. Inject scanner, clock and server for unit tests."""

    def __init__(self, config: WatcherConfig, scanner=scan_brave,
                 server=None, clock=time.monotonic, tab_opener=open_tab,
                 log_fn=None):
        self.config = config
        self.scanner = scanner
        self.server = server or ServerProcess(config, log_path=config.server_log)
        self.clock = clock
        self.tab_opener = tab_opener
        self.state = STOPPED
        self.brave_pid: int | None = None
        self.absent_since: float | None = None
        self.tab_opened = False
        self.tab_pid: int | None = None
        self.ready_failures = 0
        if self.config.remember_tab:
            self.tab_pid = self._load_tab_pid()
        self.suppress_until = 0.0
        self.restarts: list[float] = []
        self.last_start_attempt: float | None = None
        self.actions: list[str] = []
        self._log = log_fn or log.info

    # ------------------------------------------------------------ tab state
    def _load_tab_pid(self) -> int | None:
        path = self.config.state_file
        if not path:
            return None
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("tab_pid") if isinstance(data, dict) else None
        if not isinstance(pid, int) or pid <= 1:
            return None
        if not Path(f"/proc/{pid}").exists():
            return None
        return pid

    def _save_tab_pid(self) -> None:
        path = self.config.state_file
        if not path or not self.config.remember_tab:
            return
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"tab_pid": self.tab_pid}),
                              encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ utilities
    def _record(self, action: str) -> None:
        self.actions.append(action)
        self._log("state=%s %s", self.state, action)

    def _restart_allowed(self, now: float) -> bool:
        self.restarts = [stamp for stamp in self.restarts
                         if now - stamp < self.config.restart_window]
        return len(self.restarts) < self.config.max_restarts

    def _start(self, now: float, proc: ProcessInfo) -> None:
        self.last_start_attempt = now
        self._preflight()
        if not self.server.start():
            self._record("start_failed")
            return
        if not self.server.wait_ready(self.config.ready_timeout):
            self._fail_ready("ready_timeout")
            return
        self.last_start_attempt = None
        self.ready_failures = 0
        self.state = RUNNING
        self.brave_pid = proc.pid
        self.absent_since = None
        self.tab_opened = False
        # Keep the persisted tab pid: a watcher restart during the same Brave
        # session must not open a second tab.
        self._record("server_started")
        self._open_tab_once(proc.pid)

    def _open_tab_once(self, pid: int) -> None:
        """Open the app tab once for this Brave session (identified by pid)."""
        if not self.config.open_tab or self.tab_pid == pid:
            return
        env = process_display_env(pid)
        if self.tab_opener(self.config, env):
            self.tab_opened = True
            self.tab_pid = pid
            self._save_tab_pid()
            self._record("tab_opened")
        else:
            self.tab_pid = pid
            self._save_tab_pid()
            self._record("tab_failed")

    def _server_exit_code(self):
        getter = getattr(self.server, "exit_code", None)
        return getter() if callable(getter) else None

    def _server_stderr(self) -> str:
        getter = getattr(self.server, "last_stderr", None)
        return getter() if callable(getter) else ""

    def _fail_ready(self, reason: str) -> None:
        code = self._server_exit_code()
        detail = self._server_stderr()
        self.server.stop(self.config.stop_timeout)
        self.ready_failures += 1
        self._record(reason)
        if code is not None:
            self._log("server exited with code %s", code)
        if detail:
            self._log("server stderr: %s", detail.replace("\n", " | "))
        if self.ready_failures >= self.config.max_ready_failures:
            self.ready_failures = 0
            self.suppress_until = self.clock() + self.config.give_up_backoff
            self._log("server failed to become ready repeatedly; pausing "
                      "attempts for %ss", self.config.give_up_backoff)
            self.server.stop(self.config.stop_timeout)
            self.state = STOPPED
            self.brave_pid = None
            self.tab_opened = False
            self._record("gave_up")

    def _restart(self, now: float) -> None:
        self._record("server_crashed")
        detail = self._server_stderr()
        if detail:
            self._log("server stderr: %s", detail.replace("\n", " | "))
        self.server.stop(self.config.stop_timeout)
        if not self._restart_allowed(now):
            self._record("restart_suppressed")
            self._fail_ready("restart_rate_limited")
            return
        self.restarts.append(now)
        self._preflight()
        if not self.server.start():
            self._record("restart_failed")
            self._fail_ready("restart_start_failed")
            return
        if not self.server.wait_ready(self.config.ready_timeout):
            self._record("restart_ready_timeout")
            self._fail_ready("restart_not_ready")
            return
        self.ready_failures = 0
        self._record("server_restarted")

    def _stop(self) -> None:
        self.server.stop(self.config.stop_timeout)
        self.state = STOPPED
        self.brave_pid = None
        self.absent_since = None
        self.tab_opened = False
        self.tab_pid = None
        self.restarts.clear()
        self.last_start_attempt = None
        self.ready_failures = 0
        self.suppress_until = 0.0
        self._record("server_stopped")

    # ----------------------------------------------------------------- step
    def step(self) -> None:
        now = self.clock()
        procs = self.scanner()

        if self.state == STOPPED:
            if procs:
                if now < self.suppress_until:
                    return
                if (self.last_start_attempt is not None and
                        now - self.last_start_attempt < self.config.start_cooldown):
                    return
                self._start(now, procs[0])
            return

        if procs:
            pid = procs[0].pid
            if self.brave_pid != pid:
                # A new Brave session (restart) inside the grace window: keep
                # the server, but give the new session its own single tab.
                self._log("new Brave session pid=%s (was %s)", pid,
                          self.brave_pid)
                self.brave_pid = pid
                self._open_tab_once(pid)
            self.absent_since = None
            if not self.server.alive():
                self._restart(now)
            return

        if self.absent_since is None:
            self.absent_since = now
            self._record("brave_missing")
            return
        if now - self.absent_since >= self.config.grace_seconds:
            self._stop()

    def _preflight(self) -> None:
        if not port_in_use(self.config.port):
            return
        own = getattr(self.server, "proc", None)
        own_pid = getattr(own, "pid", None)
        info = status_info(self.config.port)
        own_instance = getattr(self.server, "instance_id", None)
        if info and own_pid and info.get("pid") == own_pid:
            return
        if info and own_instance and info.get("instance") == own_instance:
            return
        if kill_stale_server(self.config.port, exclude_pid=own_pid):
            self._log("cleared stale process on port %s", self.config.port)
        else:
            self._log("port %s is in use by a process this watcher will not "
                      "kill; set another port in watcher.json",
                      self.config.port)

    def run_forever(self) -> None:
        log.info("watcher starting (port=%s grace=%ss)",
                 self.config.port, self.config.grace_seconds)
        self._preflight()
        while True:
            try:
                self.step()
            except Exception:
                log.exception("watcher step failed")
            time.sleep(self.config.poll_seconds)


# --------------------------------------------------------- single instance
class InstanceLock:
    """Advisory flock so only one watcher drives the server/port at a time."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self.handle.close()
        except OSError:
            pass
        self.handle = None


# ------------------------------------------------------------------- logging
def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = RotatingFileHandler(str(log_path), maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(formatter)
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stderr))


def main(argv=None) -> int:
    config = WatcherConfig.load()
    log_dir = Path.home() / ".local" / "state" / "yt-discover"
    setup_logging(log_dir / "watcher.log")
    config.server_log = str(log_dir / "server.log")
    config.state_file = str(log_dir / "watcher-state.json")
    lock = InstanceLock(log_dir / "watcher.lock")
    if not lock.acquire():
        log.error("another yt-discover-watcher is already running; exiting")
        print("another yt-discover-watcher is already running", file=sys.stderr)
        return 1
    scanner = lambda: scan_brave(config.brave_exe_prefix,  # noqa: E731
                                 exe_name=config.brave_exe_name)
    watcher = Watcher(config, scanner=scanner)

    def shutdown(signum, _frame):
        log.info("signal %s; stopping server", signum)
        watcher.server.stop(config.stop_timeout)
        lock.release()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        watcher.run_forever()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
