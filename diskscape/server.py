"""Local HTTP server: serves the UI and a small JSON API over a Scanner."""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .scanner import Scanner, disk_usage, volumes

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
IS_MAC = sys.platform == "darwin"
# Inline style attributes are used for chart colours; everything else is same-origin only.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# Moves a path to the Trash via Finder (so "Put Back" works) and returns where it landed.
TRASH_SCRIPT = """on run argv
    tell application "Finder" to set trashed to delete (POSIX file (item 1 of argv) as alias)
    return POSIX path of (trashed as alias)
end run"""
DEFAULT_IDLE_MINUTES = 15


def osascript_path(stdout):
    """The POSIX path osascript printed. Only the line ending and a folder's trailing
    slash are trimmed: file names may legitimately start or end with spaces."""
    path = stdout[:-1] if stdout.endswith("\n") else stdout
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/nonexistent"


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class App:
    """Server state: the current scan, the per-run access token and the idle clock."""

    def __init__(self, token=None, idle_timeout=0, clock=time.time):
        self.token = token or secrets.token_urlsafe(16)
        self.port = 0
        self.scanner = None
        self.idle_timeout = idle_timeout  # seconds; 0 = never stop
        self.clock = clock  # injectable so tests don't depend on real time
        self.last_active = clock()

    def touch(self):
        self.last_active = self.clock()

    def idle_for(self):
        """Seconds since the user last did anything. A running scan is never idle."""
        s = self.scanner
        if s and s.state in ("scanning", "finalizing"):
            return 0.0
        since = max(self.last_active, (s.finished or 0) if s else 0)
        return self.clock() - since

    def scan(self, path):
        if self.scanner:
            self.scanner.cancel()
        self.scanner = Scanner(path).start()
        return self.scanner

    def done_scanner(self):
        s = self.scanner
        if not s or s.state != "done":
            raise ApiError(409, "No completed scan")
        return s


def _int(qs, key, default, hi):
    try:
        return max(0, min(int(qs.get(key, default)), hi))
    except ValueError:
        raise ApiError(400, f"Bad value for {key}")


def _float(qs, key, default):
    try:
        return max(0.0, min(float(qs.get(key, default)), 1.0))
    except ValueError:
        raise ApiError(400, f"Bad value for {key}")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Diskscape/" + __version__

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self, path):
            # Host check defeats DNS rebinding; the token defeats cross-site requests.
            if self.headers.get("Host", "") not in (f"127.0.0.1:{app.port}", f"localhost:{app.port}"):
                return False
            if path.startswith("/api/"):
                return secrets.compare_digest(self.headers.get("X-Token", ""), app.token)
            return True

        def _dispatch(self, fn):
            url = urlparse(self.path)
            if not self._authorised(url.path):
                return self._send(403, {"error": "Forbidden"})
            if url.path.startswith("/api/") and url.path != "/api/ping":
                app.touch()  # only token-bearing requests count as activity
            try:
                result = fn(url)
            except ApiError as e:
                return self._send(e.code, {"error": str(e)})
            if result is None:
                return self._send(404, {"error": "Not found"})
            if isinstance(result, tuple):  # (code, raw bytes, content type) for static files
                return self._send(*result)
            self._send(200, result)

        def do_GET(self):
            self._dispatch(self._get)

        def do_POST(self):
            self._dispatch(self._post)

        def _get(self, url):
            if url.path in STATIC_FILES:
                fname, ctype = STATIC_FILES[url.path]
                with open(os.path.join(STATIC, fname), "rb") as fh:
                    return 200, fh.read(), ctype
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/status":
                body = app.scanner.status() if app.scanner else {"state": "idle"}
                body.update(home=os.path.expanduser("~"), platform=sys.platform, version=__version__)
                return body
            if url.path == "/api/volumes":
                return volumes()
            if url.path == "/api/ping":
                # The page pings periodically; active=1 means the user interacted since the last ping.
                if qs.get("active") == "1":
                    app.touch()
                return {"ok": True, "idle_timeout": app.idle_timeout}
            if url.path not in ("/api/tree", "/api/top", "/api/types"):
                return None
            s = app.done_scanner()
            with s.lock:
                try:
                    node, f = s.find(qs.get("path", s.root_path))
                except KeyError:
                    raise ApiError(404, "Path is not in the scan")
                if f:
                    raise ApiError(400, "Not a folder")
                if url.path == "/api/top":
                    return s.largest_files(node, _int(qs, "n", 200, 2000))
                if url.path == "/api/types":
                    return s.types(node)
                body = s.tree(node, _int(qs, "depth", 1, 10), _int(qs, "k", 200, 5000),
                              node.size * _float(qs, "minfrac", 0))
            body["disk"] = disk_usage(s.root_path)
            body["root"] = s.root_path
            return body

        def _post(self, url):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                raise ApiError(400, "Bad JSON body")
            if url.path == "/api/scan":
                path = os.path.realpath(os.path.expanduser(str(data.get("path") or "~")))
                if not os.path.isdir(path):
                    raise ApiError(400, f"Not a folder: {path}")
                return app.scan(path).status()
            if url.path == "/api/cancel":
                if app.scanner:
                    app.scanner.cancel()
                return {"ok": True}
            if url.path in ("/api/reveal", "/api/trash"):
                return self._file_action(url.path, str(data.get("path", "")))
            return None

        def _file_action(self, action, path):
            if not IS_MAC:
                raise ApiError(501, "Only supported on macOS")
            s = app.done_scanner()
            path = os.path.normpath(path)
            with s.lock:
                try:
                    s.find(path)  # only ever act on paths inside the scanned tree
                except KeyError:
                    raise ApiError(404, "Path is not in the scan")
            # Re-check the live filesystem: no symlinked folders on the way, same kind of item.
            try:
                with s.lock:
                    identity = s.check_target(path, allow_symlink=action == "/api/reveal")
            except (KeyError, ValueError) as e:
                raise ApiError(409, str(e).strip("'\""))
            if action == "/api/reveal":
                subprocess.Popen(["open", "-R", path])
                return {"ok": True}
            r = subprocess.run(["osascript", "-e", TRASH_SCRIPT, path], capture_output=True, text=True)
            if r.returncode != 0:
                raise ApiError(500, r.stderr.strip() or "Finder could not move the item to the Trash")
            # Finder only takes a path, so the item could still have been swapped after the
            # check. Confirm what landed in the Trash is what we checked.
            try:
                st = os.lstat(osascript_path(r.stdout))
                same = (st.st_dev, st.st_ino) == identity
            except OSError:
                same = False
            if not same:
                raise ApiError(409, "The item changed while it was being trashed. Check the Trash "
                                    "(Put Back restores it), then rescan.")
            with s.lock:
                s.remove(path)
            return {"ok": True}

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers drop connections all the time; don't spray tracebacks for that.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def make_server(app, port, attempts=20):
    """Bind to 127.0.0.1 on `port`, or the next free port. Port 0 picks any free port."""
    for p in range(port, port + attempts) if port else [0]:
        try:
            httpd = Server(("127.0.0.1", p), make_handler(app))
        except OSError:
            continue
        app.port = httpd.server_address[1]
        return httpd
    raise OSError(f"No free port in {port}-{port + attempts - 1}")


def stop_when_idle(app, httpd, poll=None):
    """Shut the server down once app.idle_for() exceeds app.idle_timeout.

    Returns an Event that is set if the shutdown was due to inactivity.
    """
    stopped = threading.Event()
    if not app.idle_timeout:
        return stopped
    poll = poll or min(30.0, app.idle_timeout / 10)

    def watch():
        while app.idle_for() < app.idle_timeout:
            time.sleep(poll)
        stopped.set()
        httpd.shutdown()

    threading.Thread(target=watch, daemon=True).start()
    return stopped


def main(argv=None):
    ap = argparse.ArgumentParser(prog="diskscape", description="Zero-dependency disk space analyser.")
    ap.add_argument("path", nargs="?", help="folder to scan straight away (default: show the start screen)")
    ap.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765, or next free)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    ap.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_MINUTES, metavar="MIN",
                    help=f"stop after MIN minutes without activity (default: {DEFAULT_IDLE_MINUTES}; 0 = never)")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)

    if args.idle_timeout < 0:
        ap.error("--idle-timeout must be 0 or more")
    app = App(idle_timeout=args.idle_timeout * 60)
    if args.path:
        path = os.path.expanduser(args.path)
        if not os.path.isdir(path):
            ap.error(f"not a folder: {args.path}")
        app.scan(path)
    try:
        httpd = make_server(app, args.port)
    except OSError as e:
        sys.exit(str(e))

    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    idle_note = f" (or idle for {args.idle_timeout:g} min)" if args.idle_timeout else ""
    print(f"Diskscape running at {url}\nPress Ctrl+C to stop{idle_note}.", flush=True)
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    idle_stop = stop_when_idle(app, httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    if idle_stop.is_set():
        print(f"Stopped after {args.idle_timeout:g} minutes of inactivity.")
