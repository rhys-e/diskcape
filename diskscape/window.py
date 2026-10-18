"""Open the UI in its own Chromium "app" window, backed by a throwaway profile.

The separate profile means a separate browser instance with no extensions and nothing
shared with the user's normal browsing. Diskscape cleans it up when it stops: the
instance is found by its unique profile path, asked to quit, and the profile deleted.

Browser command lines are visible to other users (`ps`), so the access token is never
put on one. The window opens a private start page (in a 0700 folder, readable only by
this user) that forwards to the tokened URL, and that page is deleted once the window
has connected.
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# Tried in order. macOS finds installed apps by bundle ID, so no install paths are guessed.
MAC_BUNDLE_IDS = ("com.google.Chrome", "org.chromium.Chromium", "com.microsoft.edgemac", "com.brave.Browser")
LINUX_BINARIES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "microsoft-edge", "brave-browser")

FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",  # no update checks or field trials from the fresh profile
    "--disable-sync",
    "--disable-extensions",
    "--use-mock-keychain",  # macOS: leave the user's keychain alone
    "--password-store=basic",  # Linux equivalent
    "--window-size=1440,900",
)

START_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<title>Diskscape</title>
<script>location.replace({url})</script>
"""


LAUNCH_TIMEOUT = 30.0  # seconds to wait for `open` to hand the window over to the browser


class LaunchError(RuntimeError):
    pass


class NoBrowserError(LaunchError):
    pass


class AppWindow:
    """A Chromium --app window running on its own temporary profile.

    `url` is the full app URL including the token; it only ever goes into a private file.
    """

    def __init__(self, url, run=subprocess.run, popen=subprocess.Popen, platform=sys.platform, extra_flags=(),
                 list_processes=None):
        self.base = tempfile.mkdtemp(prefix="diskscape-")  # mode 0700
        self.profile = os.path.join(self.base, "profile")
        self.start_page = os.path.join(self.base, "start.html")
        self.list_processes = list_processes  # injectable for tests; default runs `ps`
        try:
            self._write_start_page(url)
            args = [f"--app={pathlib.Path(self.start_page).as_uri()}", f"--user-data-dir={self.profile}",
                    *FLAGS, *extra_flags]
            self.browser = self._launch(args, run, popen, platform)
        except BaseException:
            self.close(timeout=2.0)  # a launch that timed out may still have started a browser
            raise

    def _write_start_page(self, url):
        # json.dumps makes a safe JS string; also stop "</script>" from ending the block.
        page = START_PAGE.format(url=json.dumps(url).replace("</", "<\\/"))
        fd = os.open(self.start_page, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(page)

    def remove_start_page(self):
        """Delete the start page (it holds the token) once the window has used it."""
        try:
            os.remove(self.start_page)
        except FileNotFoundError:
            pass

    @staticmethod
    def _launch(args, run, popen, platform):
        if platform == "darwin":
            for bundle_id in MAC_BUNDLE_IDS:
                # -n: a new instance even if the browser is already open (it is: different profile).
                try:
                    r = run(["open", "-n", "-b", bundle_id, "--args", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=LAUNCH_TIMEOUT)
                except subprocess.TimeoutExpired:
                    raise LaunchError(f"timed out after {LAUNCH_TIMEOUT:g}s waiting for macOS to open {bundle_id}")
                if r.returncode == 0:
                    return bundle_id
        else:
            for name in LINUX_BINARIES:
                path = shutil.which(name)
                if path:
                    popen([path, *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return path
        raise NoBrowserError("Diskscape opens in its own Chrome, Chromium, Edge or Brave window, "
                             "but none of those browsers is installed.")

    def close(self, timeout=5.0, list_processes=None):
        """Quit the browser instance using this profile, then delete the temporary files.

        Returns a list of problems (empty if everything was cleaned up).
        """
        problems = []
        try:
            pids = browser_pids(self.profile, list_processes or self.list_processes)
        except (OSError, subprocess.CalledProcessError) as e:
            pids = []
            problems.append(f"couldn't list processes to quit the Diskscape browser window ({e})")
        signal_all(pids, signal.SIGTERM)
        pids = wait_for_exit(pids, timeout)
        if pids:
            signal_all(pids, signal.SIGKILL)
            pids = wait_for_exit(pids, 2.0)
        problems += [f"browser process {pid} is still running" for pid in pids]
        error = remove_tree(self.base)
        if error:
            problems.append(f"couldn't delete the temporary browser profile at {self.base} ({error})")
        return problems


def ps_lines():
    out = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=,args="],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def browser_pids(profile, list_processes=None):
    """PIDs of browser processes started with this exact profile.

    Only the main process is returned: helper processes (which carry --type=) exit with
    it. The profile path is random and unique to this run, so no other browser matches.
    """
    flag = f"--user-data-dir={profile}"
    pids = []
    for line in (list_processes or ps_lines)():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and f" {flag} " in f" {args} " and "--type=" not in args:
            pids.append(int(pid))
    return pids


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # reap it if it's our own child (Linux launches it directly)
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def wait_for_exit(pids, timeout):
    """Wait up to `timeout` seconds; return the PIDs still running."""
    deadline = time.monotonic() + timeout
    pids = [p for p in pids if alive(p)]
    while pids and time.monotonic() < deadline:
        time.sleep(0.1)
        pids = [p for p in pids if alive(p)]
    return pids


def signal_all(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass  # already gone, or not ours; wait_for_exit reports anything left running


def remove_tree(path, attempts=5):
    """Delete a folder, retrying briefly (a browser may still be flushing files as it exits).

    Returns None on success, or the last error.
    """
    error = None
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            error = e
            time.sleep(0.2)
        else:
            return None
    return error if os.path.exists(path) else None
