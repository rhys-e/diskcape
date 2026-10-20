import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

import socket
from unittest import mock

from diskscape import server
from diskscape.server import (CLOSE_GRACE, CONNECT_TIMEOUT, App, make_server, osascript_path, remove_start_page,
                              stop_when_done)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.realpath(cls._tmp.name)
        os.makedirs(f"{cls.root}/sub")
        with open(f"{cls.root}/sub/file.txt", "w") as fh:
            fh.write("hello")
        cls.app = App(token="test-token")
        cls.httpd = make_server(cls.app, 0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.app.scan(cls.root)
        deadline = time.time() + 10
        while cls.app.scanner.state != "done" and time.time() < deadline:
            time.sleep(0.01)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._tmp.cleanup()

    def request(self, method, path, body=None, token="test-token", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.app.port}"}
        if token:
            headers["X-Token"] = token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, json.loads(data) if ctype.startswith("application/json") else data

    def test_serves_ui(self):
        for path in ("/", "/app.js", "/style.css"):
            code, body = self.request("GET", path, token=None)
            self.assertEqual(code, 200, path)
            self.assertTrue(body)

    def test_api_requires_token(self):
        self.assertEqual(self.request("GET", "/api/status", token=None)[0], 403)
        self.assertEqual(self.request("GET", "/api/status", token="wrong")[0], 403)

    def test_rejects_foreign_host(self):
        self.assertEqual(self.request("GET", "/api/status", host=f"evil.example:{self.app.port}")[0], 403)
        self.assertEqual(self.request("GET", "/", token=None, host="evil.example")[0], 403)

    def test_status(self):
        code, body = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "done")
        self.assertEqual(body["root"], self.root)

    def test_tree(self):
        code, body = self.request("GET", "/api/tree?depth=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["children"][0]["name"], "sub")
        self.assertEqual(body["children"][0]["children"][0]["name"], "file.txt")

    def test_bad_params(self):
        self.assertEqual(self.request("GET", "/api/tree?depth=abc")[0], 400)
        self.assertEqual(self.request("GET", "/api/tree?path=/etc")[0], 404)
        self.assertEqual(self.request("GET", f"/api/top?path={self.root}/sub/file.txt")[0], 400)
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)

    def test_actions_limited_to_scan(self):
        code, _ = self.request("POST", "/api/trash", {"path": "/etc/hosts"})
        self.assertIn(code, (404, 501))  # 501 on non-macOS
        code, _ = self.request("POST", "/api/trash", {"path": self.root})
        self.assertIn(code, (409, 501))

    def test_security_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.app.port}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        csp = resp.getheader("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_top_zero(self):
        self.assertEqual(self.request("GET", "/api/top?n=0"), (200, []))

    def test_trash_rechecks_filesystem(self):
        if sys.platform != "darwin":
            self.skipTest("trash is macOS-only")
        # The scan saw a real file; replace it with a symlink before trashing.
        os.rename(f"{self.root}/sub/file.txt", f"{self.root}/sub/file.bak")
        os.symlink("/etc/hosts", f"{self.root}/sub/file.txt")
        try:
            code, body = self.request("POST", "/api/trash", {"path": f"{self.root}/sub/file.txt"})
            self.assertEqual(code, 409)
            self.assertIn("Symbolic links", body["error"])
        finally:
            os.remove(f"{self.root}/sub/file.txt")
            os.rename(f"{self.root}/sub/file.bak", f"{self.root}/sub/file.txt")

    def raw(self, data, timeout=5):
        """Send raw bytes; return the status line (or b"" if the server hung up)."""
        with socket.create_connection(("127.0.0.1", self.app.port), timeout=timeout) as sock:
            sock.sendall(data)
            return sock.recv(1024).split(b"\r\n")[0]

    def test_request_body_limits(self):
        host = f"Host: 127.0.0.1:{self.app.port}\r\n".encode()
        auth = b"X-Token: test-token\r\n"
        # Too large: refused from the header alone, without waiting for a body that never comes.
        self.assertIn(b"413", self.raw(b"POST /api/scan HTTP/1.1\r\n" + host + auth + b"Content-Length: 999999999\r\n\r\n"))
        self.assertIn(b"400", self.raw(b"POST /api/scan HTTP/1.1\r\n" + host + auth + b"Content-Length: -5\r\n\r\n"))
        # Without the token, the body is never read at all.
        self.assertIn(b"403", self.raw(b"POST /api/scan HTTP/1.1\r\n" + host + b"Content-Length: 999999999\r\n\r\n"))

    def test_posts_answered_without_reading_unneeded_bodies(self):
        """Each request promises a 100-byte body and never sends it: a reply proves no read."""
        host = f"Host: 127.0.0.1:{self.app.port}\r\n".encode()
        promise = b"Content-Length: 100\r\n\r\n"
        for path in (b"/", b"/app.js", b"/api/nope"):  # no token: every POST needs one
            self.assertIn(b"403", self.raw(b"POST " + path + b" HTTP/1.1\r\n" + host + promise, timeout=3), path)
        auth = b"X-Token: test-token\r\n"
        for path in (b"/", b"/api/nope", b"/api/status"):  # token, but no such POST route
            self.assertIn(b"404", self.raw(b"POST " + path + b" HTTP/1.1\r\n" + host + auth + promise, timeout=3), path)

    def test_scan_rejects_non_folder(self):
        code, body = self.request("POST", "/api/scan", {"path": f"{self.root}/sub/file.txt"})
        self.assertEqual(code, 400)
        self.assertEqual(self.request("POST", "/api/scan", ["not", "an", "object"])[0], 400)

    def test_odd_header_values_get_a_reply(self):
        host = f"Host: 127.0.0.1:{self.app.port}\r\n".encode()
        self.assertIn(b"403", self.raw(b"GET /api/status HTTP/1.1\r\n" + host + b"X-Token: \xe9\r\n\r\n"))
        self.assertIn(b"400", self.raw(b"POST /api/cancel HTTP/1.1\r\n" + host + b"X-Token: test-token\r\n"
                                       b"Content-Length: \xb2\r\n\r\n"))  # "²" passes str.isdigit()

    def test_unexpected_errors_get_an_error_reply(self):
        # Not a dropped connection, which the page would take for the server having stopped.
        with mock.patch.object(server, "volumes", side_effect=RuntimeError("boom")), \
                mock.patch.object(server.traceback, "print_exc") as print_exc:
            self.assertEqual(self.request("GET", "/api/volumes"), (500, {"error": "Unexpected error: boom"}))
        print_exc.assert_called_once()

    def test_trash_without_automation_permission_says_how_to_fix_it(self):
        if sys.platform != "darwin":
            self.skipTest("trash is macOS-only")
        denied = subprocess.CompletedProcess([], 1, "", "99:104: execution error: Not authorised to send "
                                                        "Apple events to Finder. (-1743)\n")
        with mock.patch.object(server.subprocess, "run", return_value=denied):
            code, body = self.request("POST", "/api/trash", {"path": f"{self.root}/sub/file.txt"})
        self.assertEqual(code, 403)
        self.assertIn("Reveal in Finder", body["error"])  # the no-permission way, offered first
        self.assertIn("Privacy & Security › Automation", body["error"])

    def test_actions_once_the_scanned_folder_is_gone(self):
        if sys.platform != "darwin":
            self.skipTest("reveal and trash are macOS-only")
        os.rename(self.root, self.root + "-moved")  # as if the drive was ejected, or the folder renamed
        try:
            for action in ("/api/reveal", "/api/trash"):
                code, body = self.request("POST", action, {"path": f"{self.root}/sub/file.txt"})
                self.assertEqual(code, 409, action)
                self.assertIn("no longer there", body["error"])
        finally:
            os.rename(self.root + "-moved", self.root)


class ScanTest(unittest.TestCase):
    def test_concurrent_scans_leave_one_running(self):
        made = []

        class SlowScanner:
            def __init__(self, path):
                self.cancelled = False
                made.append(self)

            def start(self):
                time.sleep(0.05)  # widens the gap between cancelling the old scan and recording this one
                return self

            def cancel(self):
                self.cancelled = True

        app = App()
        with mock.patch.object(server, "Scanner", SlowScanner):
            threads = [threading.Thread(target=app.scan, args=("/",)) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual([s for s in made if not s.cancelled], [app.scanner])


class SignalTest(unittest.TestCase):
    """Closing the terminal (SIGHUP) or `kill` (SIGTERM) must still close the window and clean up."""

    CHILD = textwrap.dedent("""
        import sys
        from diskscape import server

        class FakeWindow:
            start_page = ""

            def __init__(self, url):
                pass

            def close(self):
                open(sys.argv[1], "w").close()  # proves the cleanup ran
                return []

        server.AppWindow = FakeWindow
        server.main(["--port", "0"])
    """)

    def test_terminate_and_hangup_clean_up(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name), tempfile.TemporaryDirectory() as tmp:
                marker = os.path.join(tmp, "closed")
                proc = subprocess.Popen([sys.executable, "-c", self.CHILD, marker], cwd=REPO, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.addCleanup(proc.stdout.close)
                self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
                self.assertIn("open in its own window", proc.stdout.readline())  # up and running
                proc.send_signal(sig)
                self.assertEqual(proc.wait(10), 0)
                self.assertTrue(os.path.exists(marker), "the window was never closed")


class TimeoutTest(unittest.TestCase):
    def test_stalled_client_is_disconnected(self):
        with mock.patch.object(server, "REQUEST_TIMEOUT", 0.2):
            httpd = make_server(App(token="t"), 0)
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        with socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\n")  # ...and never finish the request
            start = time.time()
            self.assertEqual(sock.recv(1024), b"")  # server hangs up
            self.assertLess(time.time() - start, 3)


class OsascriptPathTest(unittest.TestCase):
    def test_keeps_meaningful_whitespace(self):
        self.assertEqual(osascript_path("/Users/x/.Trash/name ends in space \n"), "/Users/x/.Trash/name ends in space ")
        self.assertEqual(osascript_path("/Users/x/.Trash/ leading\n"), "/Users/x/.Trash/ leading")
        self.assertEqual(osascript_path("/Users/x/.Trash/folder /\n"), "/Users/x/.Trash/folder ")
        self.assertEqual(osascript_path("/\n"), "/")
        self.assertEqual(osascript_path(""), "/nonexistent")


class FakeClock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class IdleTest(unittest.TestCase):
    """Idle shutdown, driven by a fake clock so slow CI machines can't cause races."""

    def setUp(self):
        self.clock = FakeClock()
        self.app = App(token="t", idle_timeout=60, clock=self.clock)
        self.httpd = make_server(self.app, 0)
        self.addCleanup(self.httpd.server_close)

    def serve(self, watch=True):
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        if watch:
            stop_when_done(self.app, self.httpd, poll=0.01)
            self.addCleanup(thread.join, 5)
            self.addCleanup(self.app.request_stop, "test over")
        else:
            self.addCleanup(self.httpd.shutdown)
        return thread

    def ping(self, active):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        try:
            conn.request("GET", f"/api/ping?active={int(active)}",
                         headers={"Host": f"127.0.0.1:{self.app.port}", "X-Token": "t"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_stops_when_idle(self):
        thread = self.serve()
        self.clock.advance(59)
        time.sleep(0.1)  # several watcher polls
        self.assertTrue(thread.is_alive())
        self.clock.advance(2)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.app.stop_reason, "idle")

    def test_activity_keeps_it_alive(self):
        thread = self.serve()
        for _ in range(5):  # 200s of activity in total, well past the 60s timeout
            self.clock.advance(40)
            self.ping(active=True)
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        self.clock.advance(61)  # then it goes idle and stops
        thread.join(5)
        self.assertEqual(self.app.stop_reason, "idle")

    def test_unauthenticated_requests_do_not_count(self):
        self.serve(watch=False)
        self.clock.advance(30)
        for path, token in (("/", None), ("/nope", None), ("/api/status", None), ("/api/status", "bad")):
            conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
            headers = {"Host": f"127.0.0.1:{self.app.port}"}
            if token:
                headers["X-Token"] = token
            conn.request("GET", path, headers=headers)
            conn.getresponse().read()
            conn.close()
        self.assertEqual(self.app.idle_for(), 30)

    def test_passive_ping_does_not_count(self):
        self.serve(watch=False)
        self.clock.advance(30)
        self.ping(active=False)
        self.assertEqual(self.app.idle_for(), 30)
        self.ping(active=True)
        self.assertEqual(self.app.idle_for(), 0)

    def test_running_scan_is_never_idle(self):
        self.clock.advance(100)

        class FakeScan:
            state, finished = "scanning", None
        self.app.scanner = FakeScan()
        self.assertEqual(self.app.idle_for(), 0)
        FakeScan.state, FakeScan.finished = "done", self.clock()
        self.assertEqual(self.app.idle_for(), 0)  # clock restarts when the scan finishes
        self.clock.advance(5)
        self.assertEqual(self.app.idle_for(), 5)

    def test_disabled(self):
        self.app.idle_timeout = 0
        thread = self.serve()
        self.clock.advance(10 ** 6)
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        self.assertIsNone(self.app.stop_reason)


class WindowTest(unittest.TestCase):
    """The app window: launch-code exchange, and stopping when the window closes."""

    def setUp(self):
        self.clock = FakeClock()
        self.app = App(token="t", clock=self.clock)
        self.app.track_window = True
        self.httpd = make_server(self.app, 0)
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        stop_when_done(self.app, self.httpd, poll=0.01)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.app.request_stop, "test over")

    def open_window(self):
        """Open the event stream the way the page does; returns the socket."""
        sock = socket.create_connection(("127.0.0.1", self.app.port), timeout=5)
        sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{self.app.port}\r\nX-Token: t\r\n\r\n".encode())
        self.assertIn(b"200", sock.recv(1024).split(b"\r\n")[0])
        return sock

    def wait_for(self, condition):
        deadline = time.time() + 5
        while not condition() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_first_connection_runs_hook_once(self):
        calls = []
        self.app.on_first_window = lambda: calls.append(1)
        first = self.open_window()
        self.addCleanup(first.close)
        second = self.open_window()
        self.addCleanup(second.close)
        self.wait_for(lambda: self.app.windows == 2)
        self.assertEqual(calls, [1])

    def test_failing_hook_does_not_break_window_tracking(self):
        def broken():
            raise PermissionError("can't delete start page")
        self.app.on_first_window = broken
        with mock.patch.object(App, "warn") as warn:
            sock = self.open_window()
            self.wait_for(lambda: self.app.windows == 1)
            warn.assert_called_once()
        sock.close()
        self.wait_for(lambda: self.app.windows == 0)
        self.clock.advance(CLOSE_GRACE + 1)
        self.thread.join(5)
        self.assertEqual(self.app.stop_reason, "window closed")

    def test_start_page_hook_warns_instead_of_raising(self):
        class Window:
            start_page = "/tmp/x/start.html"

            def remove_start_page(self):
                raise PermissionError("denied")
        with mock.patch.object(App, "warn") as warn:
            remove_start_page(Window())()
        self.assertIn("holds the session token", warn.call_args[0][0])

    def test_events_require_token(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.app.port}"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()
        self.assertEqual(self.app.windows, 0)

    def test_stops_when_window_closes(self):
        sock = self.open_window()
        self.wait_for(lambda: self.app.windows == 1)
        self.clock.advance(3600)  # an open window keeps it running
        time.sleep(0.1)
        self.assertIsNone(self.app.stop_reason)
        sock.close()
        self.wait_for(lambda: self.app.windows == 0)
        self.clock.advance(CLOSE_GRACE + 1)
        self.thread.join(5)
        self.assertEqual(self.app.stop_reason, "window closed")

    def test_reload_within_grace_keeps_running(self):
        self.open_window().close()
        self.wait_for(lambda: self.app.window_gone_at is not None)
        self.clock.advance(CLOSE_GRACE / 2)
        sock = self.open_window()  # the reloaded page reconnects
        self.addCleanup(sock.close)
        self.wait_for(lambda: self.app.windows == 1)
        self.clock.advance(CLOSE_GRACE * 10)
        time.sleep(0.1)
        self.assertIsNone(self.app.stop_reason)

    def test_stops_if_window_never_connects(self):
        self.clock.advance(CONNECT_TIMEOUT + 1)
        self.thread.join(5)
        self.assertEqual(self.app.stop_reason, "no window")


if __name__ == "__main__":
    unittest.main()
