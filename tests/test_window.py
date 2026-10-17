import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diskscape.window import AppWindow, NoBrowserError, browser_pids

URL = "http://127.0.0.1:1/?t=SECRET-TOKEN"


class Result:
    def __init__(self, returncode):
        self.returncode = returncode


def fake_mac(calls=None, installed=("com.google.Chrome",)):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return Result(0 if cmd[3] in installed else 1)
    return run


class LaunchTest(unittest.TestCase):
    def window(self, **kw):
        w = AppWindow(URL, **{"run": fake_mac(), "platform": "darwin", **kw})
        self.addCleanup(w.close, list_processes=lambda: [])
        return w

    def test_mac_tries_bundle_ids_in_order(self):
        calls = []
        w = self.window(run=fake_mac(calls, installed=("org.chromium.Chromium",)))
        self.assertEqual(w.browser, "org.chromium.Chromium")
        self.assertEqual([c[3] for c in calls], ["com.google.Chrome", "org.chromium.Chromium"])
        cmd = calls[-1]
        self.assertEqual(cmd[:5], ["open", "-n", "-b", "org.chromium.Chromium", "--args"])
        self.assertIn(f"--user-data-dir={w.profile}", cmd)
        self.assertIn("--disable-extensions", cmd)

    def test_token_never_on_the_command_line(self):
        calls = []
        w = self.window(run=fake_mac(calls))
        self.assertNotIn("SECRET-TOKEN", " ".join(calls[-1]))
        self.assertIn(f"--app={pathlib.Path(w.start_page).as_uri()}", calls[-1])

    def test_start_page_is_private_and_forwards_to_the_app(self):
        w = self.window()
        self.assertEqual(os.stat(w.base).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(w.start_page).st_mode & 0o777, 0o600)
        page = pathlib.Path(w.start_page).read_text()
        self.assertIn('location.replace("http://127.0.0.1:1/?t=SECRET-TOKEN")', page)
        w.remove_start_page()
        self.assertFalse(os.path.exists(w.start_page))
        w.remove_start_page()  # idempotent

    def test_start_page_escapes_the_url(self):
        w = AppWindow('http://x/?t=a"</script><b>', run=fake_mac(), platform="darwin")
        self.addCleanup(w.close, list_processes=lambda: [])
        page = pathlib.Path(w.start_page).read_text()
        self.assertNotIn("</script><b>", page)
        self.assertIn('\\"', page)

    def test_linux_uses_path(self):
        launched = []
        which = {"chromium": "/usr/bin/chromium"}.get
        with mock.patch("diskscape.window.shutil.which", side_effect=which):
            w = AppWindow(URL, popen=lambda cmd, **kw: launched.append(cmd), platform="linux")
        self.addCleanup(w.close, list_processes=lambda: [])
        self.assertEqual(w.browser, "/usr/bin/chromium")
        self.assertEqual(launched[0][0], "/usr/bin/chromium")

    def test_no_browser_leaves_nothing_behind(self):
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(NoBrowserError):
            AppWindow(URL, run=lambda cmd, **kw: Result(1), platform="darwin")
        with mock.patch("diskscape.window.shutil.which", return_value=None), self.assertRaises(NoBrowserError):
            AppWindow(URL, platform="linux")
        new = {d for d in set(os.listdir(tempfile.gettempdir())) - before if d.startswith("diskscape-")}
        self.assertEqual(new, set())


class CleanupTest(unittest.TestCase):
    def window(self):
        return AppWindow(URL, run=fake_mac(), platform="darwin")

    def test_browser_pids_matches_only_this_profile(self):
        lines = [
            "  101 /Applications/Chrome --app=http://x --user-data-dir=/tmp/p1 --no-first-run",
            "  102 /Applications/Chrome Helper --type=renderer --user-data-dir=/tmp/p1",
            "  103 /Applications/Chrome --user-data-dir=/tmp/p10",
            "  104 /Applications/Chrome",
            "  105 /Applications/Chrome --app=http://y --user-data-dir=/tmp/p1",
        ]
        self.assertEqual(browser_pids("/tmp/p1", lambda: lines), [101, 105])

    def test_close_quits_the_browser_and_deletes_everything(self):
        w = self.window()
        flag = f"--user-data-dir={w.profile}"
        # Stand-in for the browser: a process carrying the profile flag on its command line.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", flag])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        problems = w.close(list_processes=lambda: [f"{proc.pid} {sys.executable} -c ... {flag}"])
        self.assertEqual(problems, [])
        proc.wait(5)
        self.assertFalse(os.path.exists(w.base))

    def test_reports_when_processes_cannot_be_listed(self):
        w = self.window()

        def broken():
            raise OSError("ps: operation not permitted")
        problems = w.close(list_processes=broken)
        self.assertEqual(len(problems), 1)
        self.assertIn("couldn't list processes", problems[0])
        self.assertFalse(os.path.exists(w.base))  # the files still go

    def test_reports_a_browser_that_will_not_quit(self):
        w = self.window()
        with mock.patch("diskscape.window.alive", return_value=True):
            problems = w.close(timeout=0.1, list_processes=lambda: [f"999999 chrome --user-data-dir={w.profile}"])
        self.assertIn("browser process 999999 is still running", problems)

    def test_reports_a_profile_that_cannot_be_deleted(self):
        w = self.window()
        self.addCleanup(shutil.rmtree, w.base, True)
        with mock.patch("diskscape.window.shutil.rmtree", side_effect=PermissionError("denied")), \
                mock.patch("diskscape.window.time.sleep"):
            problems = w.close(list_processes=lambda: [])
        self.assertEqual(len(problems), 1)
        self.assertIn("couldn't delete the temporary browser profile", problems[0])


if __name__ == "__main__":
    unittest.main()
