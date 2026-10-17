"""End-to-end test against a real installed Chromium browser. Opt-in:

    DISKSCAPE_BROWSER_TESTS=1 python3 -m unittest -v tests.test_browser

Headless by default. With DISKSCAPE_BROWSER_HEADLESS=0 a real window opens and the test
waits for you to close it, which checks the macOS close-the-window path by hand.
"""
import os
import signal
import sys
import threading
import time
import unittest

from diskscape.server import CLOSE_GRACE, App, make_server, stop_when_done
from diskscape.window import AppWindow, browser_pids, signal_all

ENABLED = os.environ.get("DISKSCAPE_BROWSER_TESTS") == "1"
HEADLESS = os.environ.get("DISKSCAPE_BROWSER_HEADLESS", "1") == "1"


def wait_for(condition, timeout):
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        time.sleep(0.1)
    return condition()


@unittest.skipUnless(ENABLED, "set DISKSCAPE_BROWSER_TESTS=1 to run against a real browser")
class RealBrowserTest(unittest.TestCase):
    def test_launch_connect_close_and_clean_up(self):
        app = App()
        app.track_window = True
        httpd = make_server(app, 0)
        serving = threading.Thread(target=httpd.serve_forever, daemon=True)
        serving.start()
        stop_when_done(app, httpd, poll=0.05)
        window = AppWindow(f"http://127.0.0.1:{app.port}/?t={app.token}",
                           extra_flags=["--headless=new"] if HEADLESS else [])
        app.on_first_window = window.remove_start_page
        try:
            # The file:// start page forwards to the app, which connects with the token.
            self.assertTrue(wait_for(lambda: app.windows >= 1, 30), "the window never connected")
            self.assertTrue(wait_for(lambda: not os.path.exists(window.start_page), 5),
                            "the start page (which holds the token) wasn't deleted")
            if HEADLESS:
                pids = browser_pids(window.profile)
                self.assertTrue(pids, "couldn't find the browser by its profile")
                signal_all(pids, signal.SIGTERM)  # stands in for the user closing it
            else:
                print("\n>>> Close the Diskscape window to finish the test.", file=sys.stderr, flush=True)
            serving.join(CLOSE_GRACE + (15 if HEADLESS else 120))
            self.assertEqual(app.stop_reason, "window closed")
        finally:
            app.request_stop("test over")
            serving.join(5)
            httpd.server_close()
            problems = window.close()
        self.assertEqual(problems, [])
        self.assertFalse(os.path.exists(window.base), "temporary profile left behind")


if __name__ == "__main__":
    unittest.main()
