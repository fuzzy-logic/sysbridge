import os
import tempfile
import unittest

from bridge.apps import Apps, AppsError, parse_html_meta, slugify, initials, content_type_for

PAGE = """<!doctype html><meta charset=utf-8><title> Llama &amp; Friends  Manager </title>
<meta name="description" content="Manage the llamas">
<meta content="🦙" name="app-icon">
<body>hi"""


class PureTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Llama Manager"), "llama-manager")
        self.assertEqual(slugify("  Ünïcode -- Tëst!! "), "unicode-test")
        self.assertEqual(slugify("a" * 80), "a" * 48)
        with self.assertRaises(AppsError):
            slugify("!!!")
        with self.assertRaises(AppsError):
            slugify("")

    def test_parse_meta(self):
        m = parse_html_meta(PAGE)
        self.assertEqual(m, {"title": "Llama & Friends Manager", "description": "Manage the llamas", "icon": "🦙"})
        # single-quoted and unquoted attribute values must work too
        self.assertEqual(parse_html_meta("<title>T</title><meta name=app-icon content='🧭'>")["icon"], "🧭")
        self.assertEqual(parse_html_meta("<title>T</title><meta name=app-icon content=🧭>")["icon"], "🧭")
        self.assertEqual(parse_html_meta("<title>T</title><meta name=description content=plain>")["description"], "plain")

    def test_parse_meta_fallbacks(self):
        m = parse_html_meta("<html><body>no head</body></html>", fallback_title="my app")
        self.assertEqual(m["title"], "my app")
        self.assertEqual(m["icon"], "MA")
        self.assertEqual(parse_html_meta("<title>Solo</title>")["icon"], "SO")

    def test_initials_and_types(self):
        self.assertEqual(initials("Llama Dashboard"), "LD")
        self.assertEqual(initials("x"), "X")
        self.assertEqual(content_type_for("a/b.html"), "text/html; charset=utf-8")
        self.assertEqual(content_type_for("a/b.unknown"), "application/octet-stream")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "apps")
        self.builtin = os.path.join(self.tmp.name, "repo", "dash")
        os.makedirs(self.builtin)
        with open(os.path.join(self.builtin, "index.html"), "w") as f:
            f.write("<title>Llama Dashboard</title><meta name=app-icon content=🦙>")
        self.apps = Apps(self.root, builtins={"dash": self.builtin})

    def tearDown(self):
        self.tmp.cleanup()

    def test_builtin_listed_first_and_protected(self):
        lst = self.apps.list()
        self.assertEqual([a["slug"] for a in lst], ["dash"])
        self.assertTrue(lst[0]["builtin"])
        self.assertEqual(lst[0]["title"], "Llama Dashboard")
        with self.assertRaises(AppsError) as cm:
            self.apps.uninstall("dash")
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(AppsError) as cm:
            self.apps.install_html("<title>dash</title>")
        self.assertEqual(cm.exception.status, 409)

    def test_install_replace_uninstall_trash(self):
        m = self.apps.install_html(PAGE, "upload.html")
        self.assertEqual(m["slug"], "llama-friends-manager")
        self.assertEqual(m["icon"], "🦙")
        self.assertIsNone(m["replaced"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, m["slug"], "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, m["slug"], "manifest.json")))
        self.assertEqual([a["slug"] for a in self.apps.list()], ["dash", "llama-friends-manager"])

        m2 = self.apps.install_html(PAGE.replace("hi", "v2"), "upload.html")
        self.assertTrue(m2["replaced"].startswith("llama-friends-manager-"))
        with open(os.path.join(self.root, m2["slug"], "index.html")) as f:
            self.assertIn("v2", f.read())
        trash = os.listdir(os.path.join(self.root, ".trash"))
        self.assertEqual(len(trash), 1)

        moved = self.apps.uninstall("llama-friends-manager")
        self.assertTrue(os.path.isdir(moved))
        self.assertEqual(len(os.listdir(os.path.join(self.root, ".trash"))), 2)
        self.assertEqual([a["slug"] for a in self.apps.list()], ["dash"])
        with self.assertRaises(AppsError) as cm:
            self.apps.uninstall("llama-friends-manager")
        self.assertEqual(cm.exception.status, 404)

    def test_install_rejects(self):
        for bad, status in [("", 400), ("   ", 400), ("just text no tags", 400), ("x" * (4 * 1024 * 1024 + 1) + "<b>", 413)]:
            with self.assertRaises(AppsError) as cm:
                self.apps.install_html(bad)
            self.assertEqual(cm.exception.status, status, bad[:20])
        with self.assertRaises(AppsError):
            self.apps.install_html("<html><body>no title</body></html>")  # no title, no filename
        m = self.apps.install_html("<html><body>no title</body></html>", "My_Tool.html")
        self.assertEqual(m["title"], "My Tool")
        self.assertEqual(m["slug"], "my-tool")

    def test_link_app(self):
        m = self.apps.install_link("http://127.0.0.1:8080/", "Router UI", "🧭")
        self.assertEqual(m["kind"], "link")
        self.assertEqual(m["slug"], "router-ui")
        self.assertEqual(self.apps.get("router-ui")["url"], "http://127.0.0.1:8080/")
        m = self.apps.install_link("http://localhost:8127")
        self.assertEqual(m["title"], "localhost:8127")
        for bad in ["ftp://x", "javascript:alert(1)", "", "http://"]:
            with self.assertRaises(AppsError):
                self.apps.install_link(bad, "t")
        self.assertIsNone(self.apps.resolve("router-ui", ""))  # link apps have no files

    def test_resolve_traversal(self):
        self.apps.install_html(PAGE)
        slug = "llama-friends-manager"
        idx = self.apps.resolve(slug, "")
        self.assertTrue(idx.endswith(os.path.join(slug, "index.html")))
        self.assertEqual(self.apps.resolve(slug, "/"), idx)
        self.assertEqual(self.apps.resolve(slug, "index.html"), idx)
        with open(os.path.join(self.tmp.name, "secret.txt"), "w") as f:
            f.write("s")
        for bad in ["../secret.txt", "../../secret.txt", "/etc/passwd", "manifest.json/../../secret.txt", "a\x00b", ".", ".."]:
            self.assertIsNone(self.apps.resolve(slug, bad), bad)
        # a symlink escaping the app dir is refused
        os.symlink(os.path.join(self.tmp.name, "secret.txt"), os.path.join(self.root, slug, "leak.txt"))
        self.assertIsNone(self.apps.resolve(slug, "leak.txt"))
        # manifest.json inside the dir is fine to serve
        self.assertIsNotNone(self.apps.resolve(slug, "manifest.json"))
        self.assertIsNone(self.apps.resolve("Bad Slug", ""))
        self.assertIsNone(self.apps.resolve("nope", ""))
        self.assertIsNotNone(self.apps.resolve("dash", ""))


if __name__ == "__main__":
    unittest.main()
