import os
import shutil
import tempfile
import unittest

from diskscape.scanner import Dir, Scanner, extension


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
        # blob.bin and hardlink.bin share an inode; whichever the scan reaches first
        # carries the size, and that order depends on thread timing and the filesystem.
        self.assertIn(top[0]["path"], (f"{self.root}/big/blob.bin", f"{self.root}/a/hardlink.bin"))
        self.assertEqual(top[1]["path"], f"{self.root}/a/b/c/clip.mov")

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

    def test_trashing_a_hard_link_moves_its_bytes_to_the_survivor(self):
        blob = alloc(f"{self.root}/big/blob.bin")
        root_before = self.s.root.size
        links = (f"{self.root}/big/blob.bin", f"{self.root}/a/hardlink.bin")
        for path, survivor in (links, links[::-1]):
            with self.subTest(removed=path):
                self.s = Scanner(self.root)  # fresh tree; remove() only edits the tree, not the disk
                self.s.run()
                self.s.remove(path)
                d, f = self.s.find(survivor)
                self.assertEqual(f[1], blob)  # the remaining link now carries the bytes
                self.assertEqual(self.s.root.size, root_before)  # the data still exists once
                self.assertEqual(d.files[0], f)  # and the folder's files stay sorted

    def test_trashing_every_hard_link_frees_the_bytes(self):
        blob = alloc(f"{self.root}/big/blob.bin")
        root_before = self.s.root.size
        self.s.remove(f"{self.root}/big")
        self.s.remove(f"{self.root}/a/hardlink.bin")
        self.assertEqual(self.s.root.size, root_before - blob - alloc(f"{self.root}/big"))
        self.assertEqual(self.s._links, {})

    def test_check_target_accepts_unchanged_items(self):
        st = os.lstat(f"{self.root}/a/b/c/clip.mov")
        self.assertEqual(self.s.check_target(f"{self.root}/a/b/c/clip.mov"), (st.st_dev, st.st_ino))
        self.assertTrue(self.s.check_target(f"{self.root}/a/b"))

    def test_check_target_refuses_swapped_parent(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        write(f"{outside.name}/c/clip.mov", 10)
        shutil.rmtree(f"{self.root}/a/b")
        os.symlink(outside.name, f"{self.root}/a/b")  # a/b now points outside the scan root
        with self.assertRaisesRegex(ValueError, "no longer a real folder"):
            self.s.check_target(f"{self.root}/a/b/c/clip.mov")
        self.assertTrue(os.path.exists(f"{outside.name}/c/clip.mov"))

    def test_check_target_refuses_symlinks_changes_and_root(self):
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.s.check_target(f"{self.root}/link-to-big")
        os.remove(f"{self.root}/a/b/c/clip.mov")
        os.mkdir(f"{self.root}/a/b/c/clip.mov")  # was a file at scan time
        with self.assertRaisesRegex(ValueError, "changed"):
            self.s.check_target(f"{self.root}/a/b/c/clip.mov")
        os.rmdir(f"{self.root}/a/b/c/clip.mov")
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            self.s.check_target(f"{self.root}/a/b/c/clip.mov")
        with self.assertRaises(ValueError):
            self.s.check_target(self.root)
        with self.assertRaises(KeyError):
            self.s.check_target("/etc/hosts")

    def test_scan_skips_folder_swapped_for_symlink(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        write(f"{outside.name}/secret.bin", 100_000)
        path = f"{self.root}/empty"
        st = os.lstat(path)  # what the parent's listing saw
        os.rmdir(path)
        os.symlink(outside.name, path)  # swapped before the worker opens it
        node = Dir("empty", self.s.root)
        self.s._scan_dir(node, path, (st.st_dev, st.st_ino))
        self.assertTrue(node.err)
        self.assertEqual(node.files, [])

    def test_scan_skips_folder_replaced_by_another(self):
        path = f"{self.root}/empty"
        st = os.lstat(path)
        # Move the original aside rather than deleting it: some filesystems (e.g. ext4)
        # reuse a freed inode number at once, which would make the new folder identical.
        os.rename(path, f"{self.root}/elsewhere")
        os.mkdir(path)
        write(f"{path}/new.bin", 10)
        node = Dir("empty", self.s.root)
        self.s._scan_dir(node, path, (st.st_dev, st.st_ino))
        self.assertTrue(node.err)

    def test_check_target_for_reveal_allows_symlinks(self):
        st = os.lstat(f"{self.root}/link-to-big")
        self.assertEqual(self.s.check_target(f"{self.root}/link-to-big", allow_symlink=True), (st.st_dev, st.st_ino))

    def test_largest_files_zero(self):
        self.assertEqual(self.s.largest_files(self.s.root, n=0), [])


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
