"""The catalogue check every PR runs, plus the store API."""
import json
import os
import tempfile
import unittest

from bridge.apps import Apps
from bridge.store import Store, store_root, validate

GOOD = """<!doctype html><meta charset=utf-8><title>Good App</title>
<meta name="description" content="Does a thing"><meta name="app-icon" content="✅"><meta name="app-version" content="1.2">
<meta name="app-author" content="someone"><body><script>fetch(location.origin + '/v1/health')</script>"""


class ValidateTests(unittest.TestCase):
    def test_good(self):
        self.assertEqual(validate("good-app", GOOD), [])

    def test_problems(self):
        self.assertIn("folder 'wrong' must equal the slug of the title ('good-app')", validate("wrong", GOOD))
        self.assertTrue(any("no <title>" in p for p in validate("x", "<body>nope</body>")))
        self.assertTrue(any("external" in p for p in validate("good-app", GOOD + '<script src="https://cdn.example/x.js"></script>')))
        self.assertTrue(any("external" in p for p in validate("good-app", GOOD + '<link rel=stylesheet href="//fonts.example/x.css">')))
        self.assertTrue(any("hardcodes" in p for p in validate("good-app", GOOD + "<script>fetch('http://127.0.0.1:8080/models')</script>")))
        self.assertTrue(any("hardcodes" in p for p in validate("good-app", GOOD + "<script>const u='http://localhost:8182/v1/all'</script>")))
        self.assertTrue(any("home link" in p for p in validate("good-app", GOOD + '<a href="/">home</a>')))
        self.assertEqual(validate("good-app", GOOD + '<meta name="sysbridge-home" content="none"><a href="/">home</a>'), [])
        self.assertTrue(any("app-icon" in p for p in validate("good-app", GOOD.replace('<meta name="app-icon" content="✅">', ''))))

    def test_every_catalogue_entry_is_valid(self):
        """The PR check: every store/<slug>/index.html must pass validate()."""
        root = store_root()
        entries = [n for n in sorted(os.listdir(root))] if os.path.isdir(root) else []
        self.assertTrue(entries, "store/ is empty")
        for name in entries:
            p = os.path.join(root, name, "index.html")
            self.assertTrue(os.path.isfile(p), f"store/{name}/ has no index.html")
            with open(p, encoding="utf-8") as f:
                problems = validate(name, f.read())
            self.assertEqual(problems, [], f"store/{name}: " + "; ".join(problems))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        os.makedirs(os.path.join(self.root, "good-app"))
        with open(os.path.join(self.root, "good-app", "index.html"), "w") as f:
            f.write(GOOD)
        os.makedirs(os.path.join(self.root, "bad-app"))
        with open(os.path.join(self.root, "bad-app", "index.html"), "w") as f:
            f.write("<title>Bad App</title><script src='https://x/y.js'></script>")
        self.apps = Apps(os.path.join(self.tmp.name, "apps"), builtins={})
        self.store = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_install_update(self):
        lst = self.store.list(self.apps)
        self.assertEqual([e["slug"] for e in lst], ["bad-app", "good-app"])
        good = [e for e in lst if e["slug"] == "good-app"][0]
        self.assertEqual((good["version"], good["author"], good["icon"], good["installed"]), ("1.2", "someone", "✅", False))
        self.assertEqual(good["problems"], [])
        self.assertTrue([e for e in lst if e["slug"] == "bad-app"][0]["problems"])
        m = self.store.install("good-app", self.apps)
        self.assertEqual(m["slug"], "good-app")
        self.assertEqual(m["original_filename"], "store/good-app/index.html")
        e = self.store.entry("good-app", self.apps)
        self.assertTrue(e["installed"])
        self.assertFalse(e["update_available"])
        with open(os.path.join(self.root, "good-app", "index.html"), "a") as f:
            f.write("<!-- v2 -->")
        self.assertTrue(self.store.entry("good-app", self.apps)["update_available"])
        with self.assertRaises(KeyError):
            self.store.install("nope", self.apps)
        self.assertIsNone(self.store.entry("../etc", self.apps))


if __name__ == "__main__":
    unittest.main()
