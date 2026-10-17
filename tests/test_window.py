import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diskscape.window import AppWindow, NoBrowserError, browser_pids


class Result:
    def __init__(self, returncode):
        self.returncode = returncode


class LaunchTest(unittest.TestCase):
    def test_mac_tries_bundle_ids_in_order(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return Result(0 if "org.chromium.Chromium" in cmd else 1)  # Chrome missing, Chromium present

        w = AppWindow("http://127.0.0.1:1/?launch=abc", run=run, platform="darwin")
        self.addCleanup(w.close, list_processes=lambda: [])
        self.assertEqual(w.browser, "org.chromium.Chromium")
        self.assertEqual([c[3] for c in calls], ["com.google.Chrome", "org.chromium.Chromium"])
        cmd = calls[-1]
        self.assertEqual(cmd[:5], ["open", "-n", "-b", "org.chromium.Chromium", "--args"])
        self.assertIn("--app=http://127.0.0.1:1/?launch=abc", cmd)
        self.assertIn(f"--user-data-dir={w.profile}", cmd)
        self.assertIn("--disable-extensions", cmd)
        self.assertEqual(os.stat(w.profile).st_mode & 0o777, 0o700)

    def test_linux_uses_path(self):
        launched = []
        which = {"chromium": "/usr/bin/chromium"}.get
        with mock.patch("diskscape.window.shutil.which", side_effect=which):
            w = AppWindow("http://x/", popen=lambda cmd, **kw: launched.append(cmd), platform="linux")
        self.addCleanup(w.close, list_processes=lambda: [])
        self.assertEqual(w.browser, "/usr/bin/chromium")
        self.assertEqual(launched[0][0], "/usr/bin/chromium")

    def test_no_browser(self):
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(NoBrowserError):
            AppWindow("http://x/", run=lambda cmd, **kw: Result(1), platform="darwin")
        with mock.patch("diskscape.window.shutil.which", return_value=None), self.assertRaises(NoBrowserError):
            AppWindow("http://x/", platform="linux")
        leftovers = {d for d in set(os.listdir(tempfile.gettempdir())) - before
                     if d.startswith("diskscape-browser-")}
        self.assertEqual(leftovers, set(), "temporary profile should be removed")


class CleanupTest(unittest.TestCase):
    def test_browser_pids_matches_only_this_profile(self):
        lines = [
            "  101 /Applications/Chrome --app=http://x --user-data-dir=/tmp/p1 --no-first-run",
            "  102 /Applications/Chrome Helper --type=renderer --user-data-dir=/tmp/p1",
            "  103 /Applications/Chrome --user-data-dir=/tmp/p10",
            "  104 /Applications/Chrome",
            "  105 /Applications/Chrome --app=http://y --user-data-dir=/tmp/p1",
        ]
        self.assertEqual(browser_pids("/tmp/p1", lambda: lines), [101, 105])

    def test_close_quits_the_browser_and_deletes_the_profile(self):
        w = AppWindow("http://x/", run=lambda cmd, **kw: Result(0), platform="darwin")
        flag = f"--user-data-dir={w.profile}"
        # Stand-in for the browser: a process carrying the profile flag on its command line.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", flag])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        w.close(list_processes=lambda: [f"{proc.pid} {sys.executable} -c ... {flag}"])
        proc.wait(5)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(os.path.exists(w.profile))


if __name__ == "__main__":
    unittest.main()
