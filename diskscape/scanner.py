"""Filesystem scanning and in-memory size tree."""
import heapq
import os
import queue
import shutil
import stat
import threading
import time

# Paths that would double count (APFS firmlinks, other mounts) or aren't real storage.
SKIP_PATHS = frozenset({"/System/Volumes", "/Volumes", "/dev", "/net", "/home", "/cores/.vol"})

CATEGORIES = {
    "Video": "mp4 mov m4v mkv avi webm wmv flv mpg mpeg 3gp mts braw r3d prproj fcpbundle",
    "Images": "jpg jpeg png gif heic heif webp tif tiff bmp raw cr2 cr3 nef arw dng psd ai svg ico icns exr",
    "Audio": "mp3 m4a aac wav aif aiff flac ogg opus caf alac mid midi logicx band",
    "Archives": "zip gz tgz bz2 xz 7z rar tar zst dmg iso pkg xip sparseimage sparsebundle img",
    "Code": "js mjs cjs ts tsx jsx py pyc rb go rs c h cc cpp hpp m mm swift java kt class jar "
            "json yaml yml toml css scss html map wasm o a dylib so",
    "Documents": "pdf doc docx xls xlsx ppt pptx key pages numbers txt md rtf csv epub",
    "Data": "db sqlite sqlite3 realm log dat bin pack idx npz npy pt pth ckpt safetensors gguf onnx parquet h5",
}
EXT_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts.split()}


class Dir:
    """A scanned directory. Files are kept as compact (name, bytes) tuples."""

    __slots__ = ("name", "parent", "dirs", "files", "size", "count", "err")

    def __init__(self, name, parent):
        self.name = name
        self.parent = parent
        self.dirs = []
        self.files = []
        self.size = 0
        self.count = 0
        self.err = False


def _size(item):
    return item.size if isinstance(item, Dir) else item[1]


def _dev(path):
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def extension(name):
    """Lower-case extension without the dot, or "" for none/dotfiles/implausibly long ones."""
    dot = name.rfind(".")
    if 0 < dot < len(name) - 1 and len(name) - dot <= 12:
        return name[dot + 1:].lower()
    return ""


class Scanner:
    """Walks a directory tree on a pool of threads, recording allocated sizes.

    Sizes are blocks actually allocated on disk (st_blocks * 512), so sparse files
    count for what they use. Hard-linked files are counted once, symlinks are not
    followed, and the walk stays on the starting volume.
    """

    def __init__(self, root_path, workers=12):
        self.root_path = os.path.realpath(os.path.expanduser(root_path))
        self.root = Dir(self.root_path, None)
        self.workers = workers
        self.state = "scanning"
        self.files = self.dirs = self.bytes = self.errors = 0
        self.current = self.root_path
        self.started = time.time()
        self.finished = None
        self.cancelled = False
        # Guards counters during the scan, and the tree afterwards (reads vs. remove()).
        self.lock = threading.RLock()
        # Hard-linked files: (dev, ino) -> [bytes, [(dir, name), ...]]. The first link
        # listed carries the bytes; the others are recorded as 0 so nothing counts twice.
        self._links = {}
        self._q = queue.Queue()
        root_dev = _dev(self.root_path)
        self.devs = {root_dev}
        # The macOS system and data volumes are stitched together with firmlinks.
        sys_devs = {_dev("/"), _dev("/System/Volumes/Data")} - {None}
        if root_dev in sys_devs:
            self.devs |= sys_devs

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self

    def run(self):
        """Scan synchronously. Use start() to scan in the background."""
        try:
            st = os.lstat(self.root_path)
            self._q.put((self.root, self.root_path, (st.st_dev, st.st_ino)))
        except OSError:
            self.root.err = True
            self.errors += 1
        threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        self._q.join()
        for _ in threads:
            self._q.put(None)
        if self.cancelled:
            self.state = "cancelled"
            return
        self.state = "finalizing"
        self._finalize()
        self.finished = time.time()
        self.state = "done"

    def cancel(self):
        self.cancelled = True

    def _worker(self):
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                if not self.cancelled:
                    self._scan_dir(*item)
            finally:
                self._q.task_done()

    def _scan_dir(self, node, path, identity):
        nfiles = nbytes = errs = 0
        fd = None
        try:
            # Open by handle and confirm it is the very folder seen when it was queued.
            # If it (or a folder above it) was swapped for a symlink since, the identity
            # won't match and we skip it rather than wander outside the scan root.
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != identity:
                raise OSError(f"{path} changed during the scan")
            with os.scandir(fd) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        errs += 1
                        continue
                    alloc = st.st_blocks * 512
                    if stat.S_ISDIR(st.st_mode):
                        child_path = os.path.join(path, e.name)
                        if st.st_dev not in self.devs or child_path in SKIP_PATHS:
                            continue
                        child = Dir(e.name, node)
                        child.size = alloc
                        node.dirs.append(child)
                        self._q.put((child, child_path, (st.st_dev, st.st_ino)))
                        continue
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        with self.lock:
                            rec = self._links.get(key)
                            if rec:
                                rec[1].append((node, e.name))
                                alloc = 0
                            else:
                                self._links[key] = [alloc, [(node, e.name)]]
                    node.files.append((e.name, alloc))
                    nfiles += 1
                    nbytes += alloc
        except OSError:
            node.err = True
            errs += 1
        finally:
            if fd is not None:
                os.close(fd)
        with self.lock:
            self.files += nfiles
            self.dirs += 1
            self.bytes += nbytes
            self.errors += errs
            self.current = path

    def _finalize(self):
        order, stack = [], [self.root]
        while stack:
            n = stack.pop()
            order.append(n)
            stack.extend(n.dirs)
        for n in reversed(order):  # children before parents
            n.files.sort(key=lambda f: -f[1])
            n.dirs.sort(key=lambda d: -d.size)
            n.size += sum(f[1] for f in n.files) + sum(d.size for d in n.dirs)
            n.count = len(n.files) + sum(d.count + 1 for d in n.dirs)

    # ---- queries (call only once state == "done") ---------------------

    def status(self):
        end = self.finished or time.time()
        return {
            "state": self.state,
            "root": self.root_path,
            "files": self.files,
            "dirs": self.dirs,
            "bytes": self.bytes,
            "errors": self.errors,
            "current": self.current,
            "elapsed": round(end - self.started, 2),
        }

    def path_of(self, node):
        parts = []
        while node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return os.path.join(self.root_path, *reversed(parts))

    def find(self, path):
        """Return (dir_node, None) for a folder or (parent_node, file_tuple) for a file.

        Raises KeyError if the path is not part of the scanned tree.
        """
        path = os.path.normpath(path)
        if path == self.root_path:
            return self.root, None
        if not path.startswith(self.root_path.rstrip(os.sep) + os.sep):
            raise KeyError(path)
        parts = os.path.relpath(path, self.root_path).split(os.sep)
        node = self.root
        for i, part in enumerate(parts):
            nxt = next((d for d in node.dirs if d.name == part), None)
            if nxt is None:
                if i == len(parts) - 1:
                    f = next((f for f in node.files if f[0] == part), None)
                    if f:
                        return node, f
                raise KeyError(path)
            node = nxt
        return node, None

    def tree(self, node, depth, k, min_size, path=None):
        """Serialise a subtree for the UI.

        Each folder lists at most `k` children of at least `min_size` bytes, down to
        `depth` levels; everything else is folded into one "N smaller items" entry.
        """
        path = path or self.path_of(node)
        out = {"name": node.name if node.parent else path, "path": path, "size": node.size,
               "count": node.count, "dir": True}
        if node.err:
            out["err"] = True
        if depth <= 0:
            out["more"] = bool(node.dirs or node.files)
            return out
        kids, rest_size, rest_n = [], 0, 0
        for item in heapq.merge(node.dirs, node.files, key=_size, reverse=True):
            size = _size(item)
            if len(kids) < k and size >= min_size and size > 0:
                if isinstance(item, Dir):
                    kids.append(self.tree(item, depth - 1, k, min_size, os.path.join(path, item.name)))
                else:
                    kids.append({"name": item[0], "path": os.path.join(path, item[0]), "size": size, "dir": False})
            else:
                rest_size += size
                rest_n += 1 + (item.count if isinstance(item, Dir) else 0)
        if rest_n:
            kids.append({"name": f"{rest_n:,} smaller items", "size": rest_size, "other": True, "n": rest_n})
        out["children"] = kids
        return out

    def _walk(self, node):
        stack = [(node, self.path_of(node))]
        while stack:
            n, p = stack.pop()
            yield n, p
            stack.extend((d, os.path.join(p, d.name)) for d in n.dirs)

    def largest_files(self, node, n=200):
        if n <= 0:
            return []
        heap = []
        for d, p in self._walk(node):
            for name, size in d.files:  # sorted descending, so we can stop early
                if len(heap) < n:
                    heapq.heappush(heap, (size, p, name))
                elif size > heap[0][0]:
                    heapq.heapreplace(heap, (size, p, name))
                else:
                    break
        heap.sort(reverse=True)
        return [{"name": name, "path": os.path.join(p, name), "size": size, "dir": False}
                for size, p, name in heap]

    def types(self, node, limit=60):
        exts, cats = {}, {}
        for d, _ in self._walk(node):
            for name, size in d.files:
                ext = extension(name)
                e = exts.setdefault(ext, [0, 0])
                e[0] += size
                e[1] += 1
                c = cats.setdefault(EXT_CATEGORY.get(ext, "Other"), [0, 0])
                c[0] += size
                c[1] += 1
        ext_list = sorted(({"ext": k, "size": v[0], "count": v[1], "cat": EXT_CATEGORY.get(k, "Other")}
                           for k, v in exts.items()), key=lambda x: -x["size"])[:limit]
        cat_list = sorted(({"cat": k, "size": v[0], "count": v[1]} for k, v in cats.items()),
                          key=lambda x: -x["size"])
        return {"exts": ext_list, "cats": cat_list}

    def check_target(self, path, allow_symlink=False):
        """Re-check, against the live filesystem, a path the user wants to act on.

        Walks down from the scan root one component at a time without following
        symlinks, so a folder swapped for a symlink since the scan can't redirect the
        action outside the root. Returns the item's (st_dev, st_ino) so the caller can
        confirm it acted on the same item. Raises KeyError or ValueError on mismatch.
        """
        node, f = self.find(path)
        path = os.path.normpath(path)
        if path == self.root_path:
            raise ValueError("Refusing to act on the scanned folder itself")
        if os.path.realpath(self.root_path) != self.root_path:
            raise ValueError("The scanned folder has moved since the scan")
        parts = os.path.relpath(path, self.root_path).split(os.sep)
        fd = os.open(self.root_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                try:
                    nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError:
                    raise ValueError(f"{part!r} is no longer a real folder; rescan first")
                os.close(fd)
                fd = nfd
            try:
                st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except OSError:
                raise ValueError("The item no longer exists; rescan first")
        finally:
            os.close(fd)
        if stat.S_ISLNK(st.st_mode):
            if allow_symlink:
                return st.st_dev, st.st_ino
            raise ValueError("Symbolic links can't be trashed from Diskscape; use Reveal in Finder")
        if stat.S_ISDIR(st.st_mode) != (f is None):
            raise ValueError("The item has changed since the scan; rescan first")
        return st.st_dev, st.st_ino

    def remove(self, path):
        """Drop a path from the tree (after it has been trashed) and fix up totals."""
        node, f = self.find(path)
        if f:
            parent, size, count = node, f[1], 1
            node.files.remove(f)

            def gone(d, name):
                return d is node and name == f[0]
        else:
            if node is self.root:
                raise ValueError("cannot remove the scan root")
            parent, size, count = node.parent, node.size, node.count + 1
            parent.dirs.remove(node)

            def gone(d, name):
                return self._within(d, node)
        self._add(parent, -size, -count)
        self._relink(gone)

    @staticmethod
    def _within(d, ancestor):
        while d is not None:
            if d is ancestor:
                return True
            d = d.parent
        return False

    @staticmethod
    def _add(d, size, count=0):
        while d is not None:
            d.size += size
            d.count += count
            d = d.parent

    def _relink(self, gone):
        """After a removal, hand a hard-linked file's bytes to a surviving link."""
        for key, rec in list(self._links.items()):
            size, links = rec
            keep = [link for link in links if not gone(*link)]
            if len(keep) == len(links):
                continue
            if not keep:
                del self._links[key]
                continue
            carrier_removed = keep[0] is not links[0]
            rec[1] = keep
            if carrier_removed and size:
                d, name = keep[0]
                d.files = sorted(((n, size if n == name else s) for n, s in d.files), key=lambda x: -x[1])
                self._add(d, size)


def disk_usage(path):
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": u.total, "used": u.total - u.free, "free": u.free}


def volumes():
    """Mounted volumes, with the startup disk first."""
    try:
        names = sorted(os.listdir("/Volumes"))
    except OSError:
        names = []
    # /Volumes/<startup disk name> is a symlink to "/", which gives us its real name.
    root_name = next((n for n in names if os.path.realpath(os.path.join("/Volumes", n)) == "/"), "/")
    out, seen = [], set()
    for name, path in [(root_name, "/")] + [(n, os.path.join("/Volumes", n)) for n in names]:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        usage = disk_usage(real)
        if usage:
            out.append({"name": name, "path": real, **usage})
    return out
