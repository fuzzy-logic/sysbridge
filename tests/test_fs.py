import os
import tempfile
import unittest

from bridge.fs import Fs, FsError


class FsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.root, "docs"))
        with open(os.path.join(self.root, "docs", "a.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(self.root, "docs", ".hidden"), "w") as f:
            f.write("x")
        with open(os.path.join(self.root, "bin.dat"), "wb") as f:
            f.write(b"\x00\x01\x02")
        with open(os.path.join(self.tmp.name, "secret.txt"), "w") as f:
            f.write("s")
        os.symlink(os.path.join(self.tmp.name, "secret.txt"), os.path.join(self.root, "leak"))
        self.fs = Fs(config_path=os.path.join(self.tmp.name, "none.json"), roots=[self.root], show_hidden=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roots_and_ls(self):
        r = os.path.realpath(self.root)
        self.assertEqual([x["path"] for x in self.fs.roots()], [r])
        d = self.fs.ls(r)
        self.assertEqual([e["name"] for e in d["entries"]], ["docs", "bin.dat", "leak"])   # dirs first, then by name
        self.assertIsNone(d["parent"])                                                     # a root has no parent
        sub = self.fs.ls(os.path.join(r, "docs"))
        self.assertEqual([e["name"] for e in sub["entries"]], ["a.txt"])                   # hidden omitted
        self.assertEqual(sub["parent"], r)
        self.assertEqual([e["name"] for e in self.fs.ls(os.path.join(r, "docs"), hidden=True)["entries"]], [".hidden", "a.txt"])
        leak = [e for e in d["entries"] if e["name"] == "leak"][0]
        self.assertTrue(leak["link"])

    def test_fenced(self):
        r = os.path.realpath(self.root)
        for bad, status in [(os.path.join(r, "..", "secret.txt"), 403), ("/etc/passwd", 403), ("docs/a.txt", 400), ("", 400),
                            (os.path.join(r, "leak"), 403), (os.path.join(r, "nope"), 404), (self.tmp.name, 403)]:
            with self.assertRaises(FsError, msg=bad) as cm:
                self.fs.resolve(bad)
            self.assertEqual(cm.exception.status, status, bad)

    def test_read_and_download(self):
        r = os.path.realpath(self.root)
        t = self.fs.read(os.path.join(r, "docs", "a.txt"))
        self.assertEqual((t["text"], t["binary"], t["size"]), ("hello", False, 5))
        b = self.fs.read(os.path.join(r, "bin.dat"))
        self.assertTrue(b["binary"])
        self.assertIsNone(b["text"])
        with self.assertRaises(FsError):
            self.fs.read(os.path.join(r, "docs"))
        real, size, name = self.fs.open_download(os.path.join(r, "docs", "a.txt"))
        self.assertEqual((size, name), (5, "a.txt"))

    def test_config_file_and_defaults(self):
        cfg = os.path.join(self.tmp.name, "fs.json")
        with open(cfg, "w") as f:
            f.write('{"roots": ["%s", "/nonexistent-dir"], "show_hidden": true}' % os.path.join(self.root, "docs"))
        fs = Fs(config_path=cfg)
        self.assertEqual([x["path"] for x in fs.roots()], [os.path.realpath(os.path.join(self.root, "docs"))])
        self.assertTrue(fs.show_hidden())
        fs2 = Fs(config_path=os.path.join(self.tmp.name, "missing.json"))
        self.assertEqual([x["path"] for x in fs2.roots()], [os.path.realpath(os.path.expanduser("~"))])   # default: home


if __name__ == "__main__":
    unittest.main()
