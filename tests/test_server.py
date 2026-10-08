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


class IdleTest(unittest.TestCase):
    def start(self, timeout):
        app = App(token="t", idle_timeout=timeout)
        httpd = make_server(app, 0)
        stopped = stop_when_idle(app, httpd, poll=0.02)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        return app, thread, stopped

    def ping(self, app, active):
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
        conn.request("GET", f"/api/ping?active={int(active)}",
                     headers={"Host": f"127.0.0.1:{app.port}", "X-Token": "t"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_stops_when_idle(self):
        app, thread, stopped = self.start(0.2)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stopped.is_set())

    def test_activity_keeps_it_alive(self):
        app, thread, stopped = self.start(0.4)
        for _ in range(6):  # 0.6s of activity, longer than the timeout
            self.ping(app, active=True)
            time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        thread.join(5)  # then it goes idle and stops
        self.assertTrue(stopped.is_set())

    def test_passive_ping_does_not_count(self):
        app = App(token="t", idle_timeout=60)
        httpd = make_server(app, 0)
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        app.last_active -= 30
        self.ping(app, active=False)
        self.assertGreaterEqual(app.idle_for(), 30)
        self.ping(app, active=True)
        self.assertLess(app.idle_for(), 1)

    def test_running_scan_is_never_idle(self):
        app = App(idle_timeout=1)
        app.last_active -= 100

        class FakeScan:
            state, finished = "scanning", None
        app.scanner = FakeScan()
        self.assertEqual(app.idle_for(), 0)
        FakeScan.state, FakeScan.finished = "done", time.time()
        self.assertLess(app.idle_for(), 1)  # clock restarts when the scan finishes

    def test_disabled(self):
        app = App(idle_timeout=0)
        self.assertFalse(stop_when_idle(app, None).is_set())


if __name__ == "__main__":
    unittest.main()
