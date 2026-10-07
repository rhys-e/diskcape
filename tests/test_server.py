import http.client
import json
import os
import tempfile
import threading
import time
import unittest

from diskscape.server import App, make_server


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


if __name__ == "__main__":
    unittest.main()
