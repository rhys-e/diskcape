import os
import tempfile
import unittest

from diskscape.scanner import Scanner, extension


def write(path, nbytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(os.urandom(nbytes))


def alloc(path):
    return os.lstat(path).st_blocks * 512


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        r = self.root
        write(f"{r}/big/blob.bin", 2_000_000)
        write(f"{r}/a/b/c/clip.mov", 500_000)
        for i in range(50):
            write(f"{r}/a/small{i}.txt", 10)
        os.makedirs(f"{r}/empty")
        os.link(f"{r}/big/blob.bin", f"{r}/a/hardlink.bin")
        os.symlink(f"{r}/big", f"{r}/link-to-big")
        self.s = Scanner(r)
        self.s.run()

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_completes(self):
        st = self.s.status()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["root"], self.root)
        self.assertEqual(st["errors"], 0)

    def test_folder_size_is_sum_of_contents(self):
        def check(node):
            own = node.size - sum(f[1] for f in node.files) - sum(d.size for d in node.dirs)
            self.assertGreaterEqual(own, 0)
            for d in node.dirs:
                check(d)
        check(self.s.root)

    def test_hard_links_counted_once(self):
        blob = alloc(f"{self.root}/big/blob.bin")
        sizes = [f[1] for d, _ in self.s._walk(self.s.root) for f in d.files if f[0].endswith(".bin")]
        self.assertEqual(sorted(sizes), [0, blob])

    def test_symlinks_not_followed(self):
        node, f = self.s.find(f"{self.root}/link-to-big")
        self.assertIsNotNone(f, "symlink should be recorded as a file entry")
        self.assertLess(f[1], 100_000)

    def test_find(self):
        node, f = self.s.find(f"{self.root}/a/b")
        self.assertEqual((node.name, f), ("b", None))
        node, f = self.s.find(f"{self.root}/a/b/c/clip.mov")
        self.assertEqual((node.name, f[0]), ("c", "clip.mov"))
        for bad in ("/etc", f"{self.root}/nope", f"{self.root}/../x", self.root + "-sibling"):
            with self.assertRaises(KeyError, msg=bad):
                self.s.find(bad)

    def test_tree_folds_small_items(self):
        t = self.s.tree(self.s.root, depth=2, k=3, min_size=0)
        self.assertEqual(t["size"], self.s.root.size)
        a = next(c for c in t["children"] if c["name"] == "a")
        self.assertEqual(len(a["children"]), 4)  # 3 shown + "smaller items"
        other = a["children"][-1]
        self.assertTrue(other["other"])
        self.assertEqual(sum(c["size"] for c in a["children"]), a["size"] - alloc(f"{self.root}/a"))
        sizes = [c["size"] for c in a["children"][:-1]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_tree_depth_limit(self):
        t = self.s.tree(self.s.root, depth=1, k=100, min_size=0)
        a = next(c for c in t["children"] if c["name"] == "a")
        self.assertNotIn("children", a)
        self.assertTrue(a["more"])

    def test_largest_files(self):
        top = self.s.largest_files(self.s.root, n=2)
        self.assertEqual([f["name"] for f in top], ["blob.bin", "clip.mov"])
        self.assertEqual(top[0]["path"], f"{self.root}/big/blob.bin")

    def test_types(self):
        t = self.s.types(self.s.root)
        exts = {e["ext"]: e for e in t["exts"]}
        self.assertEqual(exts["txt"]["count"], 50)
        self.assertEqual(exts["mov"]["cat"], "Video")
        self.assertEqual(sum(c["size"] for c in t["cats"]), sum(e["size"] for e in t["exts"]))

    def test_remove_updates_ancestors(self):
        before_root, before_a = self.s.root.size, self.s.find(f"{self.root}/a")[0].size
        clip = alloc(f"{self.root}/a/b/c/clip.mov")
        self.s.remove(f"{self.root}/a/b/c/clip.mov")
        self.assertEqual(self.s.root.size, before_root - clip)
        self.assertEqual(self.s.find(f"{self.root}/a")[0].size, before_a - clip)
        b = self.s.find(f"{self.root}/a/b")[0]
        self.s.remove(f"{self.root}/a/b")
        self.assertEqual(self.s.root.size, before_root - clip - b.size)
        with self.assertRaises(KeyError):
            self.s.find(f"{self.root}/a/b")
        with self.assertRaises(ValueError):
            self.s.remove(self.root)


class ExtensionTest(unittest.TestCase):
    def test_extension(self):
        self.assertEqual(extension("photo.JPG"), "jpg")
        self.assertEqual(extension("archive.tar.gz"), "gz")
        self.assertEqual(extension(".zshrc"), "")
        self.assertEqual(extension("Makefile"), "")
        self.assertEqual(extension("trailing."), "")
        self.assertEqual(extension("x.thisisfartoolong"), "")


if __name__ == "__main__":
    unittest.main()
