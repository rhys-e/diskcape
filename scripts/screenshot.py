#!/usr/bin/env python3
"""Render the README screenshots from a synthetic home folder.

No real files are scanned: a plausible-looking tree is built in memory and served by
the real Diskscape server, then captured with Chrome for Testing's headless shell
(https://googlechromelabs.github.io/chrome-for-testing/).

    python3 scripts/screenshot.py --chrome /path/to/chrome-headless-shell

Writes docs/screenshot-dark.png (sunburst) and docs/screenshot-light.png (treemap).
"""
import argparse
import os
import random
import subprocess
import sys
import tempfile
import threading
import time

HOME = "/Users/demo"
os.environ["HOME"] = HOME  # the UI shows paths relative to ~
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from diskscape import server  # noqa: E402
from diskscape.scanner import Dir, Scanner  # noqa: E402

KB, MB, GB = 10**3, 10**6, 10**9
WORDS = ("draft final export render backup notes report invoice photo clip scan "
         "mix take session archive build release asset sample frame shot").split()


def F(n, total, exts, names=()):
    """n files totalling roughly `total` bytes, with a long-tailed size spread."""
    return ("files", n, total, exts.split(), names)


def uuid(rng):
    h = "".join(rng.choice("0123456789ABCDEF") for _ in range(32))
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def project(rng, scale):
    return {
        "node_modules": {f"pkg-{i}": F(rng.randrange(20, 120), rng.uniform(1, 40) * MB * scale, "js json map ts md")
                         for i in range(40)},
        ".git": {"objects": {"pack": F(4, 300 * MB * scale, "pack idx")}},
        "src": F(180, 12 * MB, "ts tsx css json"),
        "dist": F(30, 60 * MB * scale, "js map css"),
    }


def spec(rng):
    return {
        "Library": {
            "Developer": {
                "CoreSimulator": {"Devices": {uuid(rng): {"data": F(300, rng.uniform(1.5, 5) * GB, "db plist dat png")}
                                              for _ in range(9)}},
                "Xcode": {
                    "DerivedData": {f"{p}-{''.join(rng.choice('abcdefghijklmnop') for _ in range(12))}":
                                    {"Build": F(400, rng.uniform(1, 6) * GB, "o a swiftmodule dylib")}
                                    for p in ("Weather", "Notes", "Tracker", "Studio")},
                    "iOS DeviceSupport": {f"iPhone16,2 18.{i}": F(60, rng.uniform(3, 5) * GB, "dylib a")
                                          for i in range(3)},
                    "Archives": F(6, 4 * GB, "xcarchive"),
                },
            },
            "Containers": {
                "com.docker.docker": {"Data": {"vms": {"0": {"data": {"Docker.raw": 41 * GB}}}}},
                "com.apple.mail": F(2000, 2.6 * GB, "emlx"),
            },
            "Caches": {
                "com.spotify.client": F(300, 6.5 * GB, "file"),
                "Homebrew": F(150, 3.1 * GB, "gz zst"),
                "Google": F(800, 2.2 * GB, "data"),
                "pip": F(900, 1.4 * GB, "whl"),
                "Yarn": F(3000, 2.8 * GB, "tgz"),
            },
            "Application Support": {
                "Steam": {"steamapps": {"common": {g: F(400, rng.uniform(6, 18) * GB, "pak bin dat")
                                                   for g in ("Skyline", "Hollow Peaks", "Orbit")}}},
                "Slack": F(500, 1.8 * GB, "db log json"),
                "Code": F(700, 1.2 * GB, "json db vsix"),
            },
            "Messages": {"Attachments": F(1500, 7.5 * GB, "heic jpg mov pdf")},
        },
        "Movies": {
            "Trip 2025.fcpbundle": F(120, 38 * GB, "mov braw"),
            "Screen Recordings": F(40, 9 * GB, "mov"),
            "_": F(25, 11 * GB, "mp4 mkv"),
        },
        "Pictures": {
            "Photos Library.photoslibrary": {"originals": {c: F(1200, rng.uniform(5, 11) * GB, "heic jpg mov")
                                                           for c in "0123456789ABCDEF"}},
            "Exports": F(300, 3 * GB, "jpg png tif"),
        },
        "Developer": {
            **{name: project(rng, scale) for name, scale in
               (("storefront", 3), ("design-system", 2), ("api-gateway", 1.5), ("blog", 0.5), ("dotfiles", 0.1))},
            "ml-experiments": {"models": F(4, 22 * GB, "gguf safetensors"), **project(rng, 1)},
        },
        "Downloads": F(260, 19 * GB, "dmg zip pkg pdf iso mp4"),
        "Music": {"Music": {"Media.localized": F(2400, 14 * GB, "m4a mp3 flac")}},
        "Documents": F(1800, 7 * GB, "pdf docx xlsx pages key numbers txt"),
        "Desktop": F(90, 1.6 * GB, "png pdf mov"),
        ".Trash": F(60, 4.2 * GB, "dmg zip mov"),
        ".cache": {"huggingface": F(12, 9 * GB, "safetensors bin json")},
        ".npm": F(5000, 2.4 * GB, "tgz json"),
    }


def subdir(node, name):
    child = Dir(name, node)
    child.size = 4096
    node.dirs.append(child)
    return child


def build(node, tree, rng):
    """Folders are dicts, F(...) is a folder of generated files ("_" = this folder), ints are files."""
    for name, val in tree.items():
        if isinstance(val, dict):
            build(subdir(node, name), val, rng)
        elif isinstance(val, tuple):
            _, n, total, exts, names = val
            target = node if name == "_" else subdir(node, name)
            weights = [rng.lognormvariate(0, 1.7) for _ in range(n)]
            s = sum(weights)
            for i, w in enumerate(weights):
                fname = names[i] if i < len(names) else \
                    f"{rng.choice(WORDS)}-{rng.randrange(100, 9999)}.{rng.choice(exts)}"
                target.files.append((fname, -(-int(total * w / s) // 4096) * 4096))
        else:
            node.files.append((name, int(val)))


def demo_scanner():
    rng = random.Random(7)
    s = Scanner(HOME)
    build(s.root, spec(rng), rng)
    s._finalize()
    stack = [s.root]
    while stack:
        d = stack.pop()
        s.dirs += 1
        s.files += len(d.files)
        stack.extend(d.dirs)
    s.bytes = s.root.size
    s.finished = time.time()
    s.started = s.finished - 23.4
    s.state = "done"
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", required=True, help="path to chrome-headless-shell")
    ap.add_argument("--out", default=os.path.join(REPO, "docs"))
    args = ap.parse_args()

    scanner = demo_scanner()
    total = 994.66 * GB
    used = scanner.root.size + 212 * GB  # the rest of the disk: system, other users…
    server.disk_usage = lambda path: {"total": int(total), "used": int(used), "free": int(total - used)}
    app = server.App(idle_timeout=0)
    app.scanner = scanner
    httpd = server.make_server(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{app.port}/?t={app.token}"

    os.makedirs(args.out, exist_ok=True)
    shots = [("screenshot-dark.png", "sunburst", "0"), ("screenshot-light.png", "treemap", "1")]
    for fname, view, scheme in shots:
        out = os.path.join(args.out, fname)
        with tempfile.TemporaryDirectory() as profile:
            subprocess.run([
                args.chrome, "--headless", "--hide-scrollbars", "--no-first-run",
                f"--user-data-dir={profile}", "--window-size=1440,900", "--force-device-scale-factor=2",
                f"--blink-settings=preferredColorScheme={scheme}",  # 0 = dark, 1 = light
                "--virtual-time-budget=5000", f"--screenshot={out}", f"{base}#view={view}",
            ], check=True, capture_output=True)
        print("wrote", os.path.relpath(out, REPO))
    httpd.shutdown()


if __name__ == "__main__":
    main()
