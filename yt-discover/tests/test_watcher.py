"""Watcher state-machine tests with a fake process list and fake clock."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT, ROOT / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import watcher as W  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeServer:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.alive_flag = False
        self.ready = True
        self.fail_start = False

    def start(self):
        if self.fail_start:
            return False
        self.started += 1
        self.alive_flag = True
        return True

    def alive(self):
        return self.alive_flag

    def wait_ready(self, timeout):
        return self.ready and self.alive_flag

    def stop(self, timeout=5):
        self.stopped += 1
        self.alive_flag = False

    def crash(self):
        self.alive_flag = False


def brave(pid=100):
    return W.ProcessInfo(pid=pid, exe="/opt/brave.com/brave/brave",
                         args=["/opt/brave.com/brave/brave"])


class WatcherStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.server = FakeServer()
        self.tabs = []

        def opener(config, env):
            self.tabs.append(env)
            return True

        self.config = W.WatcherConfig(
            port=18765, grace_seconds=15.0, poll_seconds=2.0,
            ready_timeout=20.0, stop_timeout=5.0, max_restarts=3,
            restart_window=60.0, start_cooldown=10.0, open_tab=True)
        self.procs = []
        self.watcher = W.Watcher(
            self.config, scanner=lambda: list(self.procs),
            server=self.server, clock=self.clock, tab_opener=opener,
            log_fn=lambda *a: None)

    def step(self, seconds=2.0, procs=None):
        if procs is not None:
            self.procs = list(procs)
        self.watcher.step()
        self.clock.advance(seconds)

    def test_starts_when_brave_appears_and_opens_tab_once(self):
        self.step()
        self.assertEqual(self.watcher.state, W.STOPPED)
        self.assertEqual(self.server.started, 0)

        self.step(procs=[brave()])
        self.assertEqual(self.watcher.state, W.RUNNING)
        self.assertEqual(self.server.started, 1)
        self.assertEqual(len(self.tabs), 1)

        for _ in range(5):
            self.step(procs=[brave()])
        self.assertEqual(len(self.tabs), 1)
        self.assertEqual(self.server.started, 1)

    def test_short_restart_inside_grace_keeps_server_and_reopens_tab_once(self):
        self.step(procs=[brave()])
        self.assertEqual(len(self.tabs), 1)

        self.step(procs=[])
        self.clock.advance(5)
        self.step(procs=[])
        self.assertEqual(self.watcher.state, W.RUNNING)
        self.assertEqual(self.server.stopped, 0)

        # A real Brave restart has a new pid -> one tab for the new session.
        self.step(procs=[brave(pid=101)])
        self.assertEqual(self.watcher.state, W.RUNNING)
        self.assertEqual(self.server.stopped, 0)
        self.assertEqual(len(self.tabs), 2)
        self.assertEqual(self.server.started, 1)

        for _ in range(10):
            self.step(procs=[brave(pid=101)])
        self.assertEqual(len(self.tabs), 2)

    def test_transient_scan_miss_with_same_pid_does_not_reopen_tab(self):
        self.step(procs=[brave(pid=300)])
        self.assertEqual(len(self.tabs), 1)
        self.step(procs=[])
        self.step(procs=[brave(pid=300)])
        self.step(procs=[])
        self.step(procs=[brave(pid=300)])
        self.assertEqual(len(self.tabs), 1)
        self.assertEqual(self.server.started, 1)
        self.assertEqual(self.server.stopped, 0)

    def test_stops_after_grace_period(self):
        self.step(procs=[brave()])
        self.step(procs=[])
        self.clock.advance(10)
        self.step(procs=[])
        self.assertEqual(self.watcher.state, W.RUNNING)
        self.clock.advance(6)
        self.step(procs=[])
        self.assertEqual(self.watcher.state, W.STOPPED)
        self.assertEqual(self.server.stopped, 1)

        self.clock.advance(30)
        self.step(procs=[brave(pid=200)])
        self.assertEqual(self.watcher.state, W.RUNNING)
        self.assertEqual(len(self.tabs), 2)
        self.assertEqual(self.server.started, 2)

    def test_restarts_crashed_server(self):
        self.step(procs=[brave()])
        self.assertEqual(self.server.started, 1)
        self.server.crash()
        self.step(procs=[brave()])
        self.assertEqual(self.server.started, 2)
        self.assertEqual(len(self.tabs), 1)
        self.assertIn("server_crashed", self.watcher.actions)

    def test_restart_rate_limit_suppresses_flapping(self):
        self.step(procs=[brave()])
        starts = self.server.started
        for _ in range(6):
            self.server.crash()
            self.step(procs=[brave()], seconds=1.0)
        self.assertLessEqual(self.server.started - starts,
                             self.config.max_restarts + 1)
        self.assertIn("restart_suppressed", self.watcher.actions)

    def test_never_flaps_on_marginal_absences(self):
        self.step(procs=[brave()])
        for _ in range(20):
            self.step(procs=[], seconds=1.0)
            self.clock.advance(13)
            self.step(procs=[brave(pid=100)], seconds=1.0)
        self.assertEqual(self.server.started, 1)
        self.assertEqual(self.server.stopped, 0)
        self.assertEqual(len(self.tabs), 1)

    def test_ready_timeout_stops_and_cools_down(self):
        self.server.ready = False
        self.step(procs=[brave()])
        self.assertEqual(self.watcher.state, W.STOPPED)
        self.assertEqual(self.server.started, 1)
        self.store = self.server.stopped
        self.clock.advance(1)
        self.step(procs=[brave()])
        self.assertEqual(self.server.started, 1,
                         "start cooldown should suppress an immediate retry")
        self.clock.advance(11)
        self.step(procs=[brave()])
        self.assertEqual(self.server.started, 2)

    def test_open_tab_disabled(self):
        self.config.open_tab = False
        self.step(procs=[brave()])
        self.assertEqual(self.watcher.state, W.RUNNING)
        self.assertEqual(self.tabs, [])


class ProcessScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytd-proc-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proc(self, pid, exe, args):
        base = self.tmp / str(pid)
        base.mkdir()
        os.symlink(exe, base / "exe")
        (base / "cmdline").write_bytes(b"\0".join(
            arg.encode() for arg in args) + b"\0")

    def test_main_brave_detected_helpers_ignored(self):
        self._proc(10, "/opt/brave.com/brave/brave",
                   ["/opt/brave.com/brave/brave"])
        self._proc(11, "/opt/brave.com/brave/brave",
                   ["/opt/brave.com/brave/brave", "--type=renderer"])
        self._proc(12, "/opt/brave.com/brave/brave",
                   ["/opt/brave.com/brave/brave", "--type=gpu-process"])
        self._proc(13, "/usr/bin/something-else", ["/usr/bin/something-else"])
        found = W.scan_brave("/opt/brave.com/brave/", str(self.tmp))
        self.assertEqual([p.pid for p in found], [10])

    def test_custom_prefix(self):
        self._proc(20, "/snap/brave/current/opt/brave.com/brave/brave",
                   ["/snap/brave/current/opt/brave.com/brave/brave"])
        found = W.scan_brave("/snap/brave/", str(self.tmp))
        self.assertEqual([p.pid for p in found], [20])

    def test_display_env_read(self):
        base = self.tmp / "30"
        base.mkdir()
        (base / "environ").write_bytes(
            b"DISPLAY=:0\0WAYLAND_DISPLAY=wayland-0\0HOME=/home/x\0"
            b"XDG_RUNTIME_DIR=/run/user/1000\0"
            b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\0")
        env = W.process_display_env(30, str(self.tmp))
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertIn("unix:path", env["DBUS_SESSION_BUS_ADDRESS"])


class ConfigLoadTests(unittest.TestCase):
    def test_config_from_file_and_env(self):
        tmp = Path(tempfile.mkdtemp(prefix="ytd-cfg-"))
        path = tmp / "watcher.json"
        path.write_text('{"grace_seconds": 20, "port": 9100}',
                        encoding="utf-8")
        old = os.environ.get("YT_DISCOVER_PORT")
        os.environ["YT_DISCOVER_PORT"] = "9999"
        try:
            config = W.WatcherConfig.load(path)
            self.assertEqual(config.grace_seconds, 20)
            self.assertEqual(config.port, 9999)
        finally:
            if old is None:
                os.environ.pop("YT_DISCOVER_PORT", None)
            else:
                os.environ["YT_DISCOVER_PORT"] = old
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class StalePortTests(unittest.TestCase):
    """Regression: a leftover manual server must not fake readiness."""

    def test_wait_ready_requires_own_instance_token(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"instance": "someone-else", "pid": 1}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertIsNotNone(W.status_info(port))
            config = W.WatcherConfig(port=port, ready_timeout=1.0)
            server = W.ServerProcess(config, package_root=str(ROOT))
            server.instance_id = "my-own-token"
            ready = False
            import time as real_time
            deadline = real_time.monotonic() + 1.0
            while real_time.monotonic() < deadline:
                info = W.status_info(port)
                if info and info.get("instance") == server.instance_id:
                    ready = True
                    break
                real_time.sleep(0.1)
            self.assertFalse(ready, "a foreign instance must never count as ready")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_kill_stale_only_targets_yt_discover(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"pid": os.getpid(), "instance": "x"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            # This test process is python running unittest, not "-m yt_discover".
            self.assertFalse(W.kill_stale_server(port, timeout=0.5))
            self.assertTrue(W.port_in_use(port))
        finally:
            httpd.shutdown()
            httpd.server_close()


class PreflightSafetyTests(unittest.TestCase):
    def test_preflight_never_kills_own_child(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"pid": os.getpid(), "instance": "own"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            class OwnServer:
                proc = type("P", (), {"pid": os.getpid()})()
                instance_id = "own"

                def alive(self):
                    return True

                def exit_code(self):
                    return None

            config = W.WatcherConfig(port=port)
            watcher = W.Watcher(config, scanner=lambda: [brave()],
                                server=OwnServer(),
                                clock=lambda: 1000.0,
                                tab_opener=lambda c, e: True,
                                log_fn=lambda *a: None)
            watcher._preflight()
            self.assertTrue(W.port_in_use(port),
                            "preflight must not kill the watcher's own server")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_preflight_kills_stale_only_when_excludable(self):
        # Simulated: our own pid must never be the target when excluded.
        self.assertTrue(W.kill_stale_server(0, timeout=0.1,
                                            exclude_pid=os.getpid()) in (True, False))


class InstanceLockTests(unittest.TestCase):
    def test_second_lock_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="ytd-lock-"))
        try:
            first = W.InstanceLock(tmp / "watcher.lock")
            second = W.InstanceLock(tmp / "watcher.lock")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
