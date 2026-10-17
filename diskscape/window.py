"""Open the UI in its own Chromium "app" window, backed by a throwaway profile.

The separate profile means a separate browser instance with no extensions and nothing
shared with the user's normal browsing. Diskscape cleans it up when it stops: the
instance is found by its unique profile path, asked to quit, and the profile deleted.
"""
import os
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


class NoBrowserError(RuntimeError):
    pass


class AppWindow:
    """A Chromium --app window running on its own temporary profile."""

    def __init__(self, url, run=subprocess.run, popen=subprocess.Popen, platform=sys.platform):
        self.profile = tempfile.mkdtemp(prefix="diskscape-browser-")  # private: mode 0700
        args = [f"--app={url}", f"--user-data-dir={self.profile}", *FLAGS]
        try:
            self.browser = self._launch(args, run, popen, platform)
        except BaseException:
            shutil.rmtree(self.profile, ignore_errors=True)
            raise

    @staticmethod
    def _launch(args, run, popen, platform):
        if platform == "darwin":
            for bundle_id in MAC_BUNDLE_IDS:
                # -n: a new instance even if the browser is already open (it is: different profile).
                r = run(["open", "-n", "-b", bundle_id, "--args", *args],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        """Quit the browser instance using this window's profile, then delete the profile."""
        pids = browser_pids(self.profile, list_processes)
        signal_all(pids, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while pids and time.monotonic() < deadline:
            time.sleep(0.1)
            pids = [p for p in pids if alive(p)]
        signal_all(pids, signal.SIGKILL)
        shutil.rmtree(self.profile, ignore_errors=True)


def ps_lines():
    try:
        out = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=,args="],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
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


def signal_all(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
