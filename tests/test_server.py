import http.client
import json
import os
import tempfile
import threading
import time
import unittest

from diskscape.server import App, make_server, stop_when_idle


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
        self.assertIn(code, (400, 501))

    def test_scan_rejects_non_folder(self):
        code, body = self.request("POST", "/api/scan", {"path": f"{self.root}/sub/file.txt"})
        self.assertEqual(code, 400)
        self.assertEqual(self.request("POST", "/api/scan", ["not", "an", "object"])[0], 400)


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
        stopped = stop_when_idle(self.app, self.httpd, poll=0.01) if watch else None
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        if not watch:
            self.addCleanup(self.httpd.shutdown)
        return thread, stopped

    def ping(self, active):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        try:
            conn.request("GET", f"/api/ping?active={int(active)}",
                         headers={"Host": f"127.0.0.1:{self.app.port}", "X-Token": "t"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_stops_when_idle(self):
        thread, stopped = self.serve()
        self.clock.advance(59)
        time.sleep(0.1)  # several watcher polls
        self.assertTrue(thread.is_alive())
        self.clock.advance(2)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stopped.is_set())

    def test_activity_keeps_it_alive(self):
        thread, stopped = self.serve()
        for _ in range(5):  # 200s of activity in total, well past the 60s timeout
            self.clock.advance(40)
            self.ping(active=True)
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        self.clock.advance(61)  # then it goes idle and stops
        thread.join(5)
        self.assertTrue(stopped.is_set())

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
        self.assertFalse(stop_when_idle(self.app, self.httpd).is_set())


if __name__ == "__main__":
    unittest.main()
