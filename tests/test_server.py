"""CORS, token and action gating over a real socket on an ephemeral port."""
import http.client
import json
import os
import tempfile
import threading
import unittest

from bridge.server import Bridge, Config


class ServerFixture:
    def __init__(self, actions_json=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.actions_path = os.path.join(self.tmp.name, "actions.json")
        if actions_json is not None:
            with open(self.actions_path, "w") as f:
                json.dump(actions_json, f)
        self.token_file = os.path.join(self.tmp.name, "rt", "token")
        self.apps_root = os.path.join(self.tmp.name, "apps")
        self.builtin_dir = os.path.join(self.tmp.name, "builtin", "dash")
        os.makedirs(self.builtin_dir)
        with open(os.path.join(self.builtin_dir, "index.html"), "w") as f:
            f.write("<title>Llama Dashboard</title><meta name=app-icon content=🦙><script src=app.js></script>")
        with open(os.path.join(self.builtin_dir, "app.js"), "w") as f:
            f.write("// js")
        cfg = Config(bind="127.0.0.1", port=0, extra_origins=["https://allowed.example"],
                     actions_file=self.actions_path, token_file=self.token_file,
                     apps_root=self.apps_root, builtins={"dash": self.builtin_dir})
        self.srv = Bridge(cfg)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def request(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = data
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), parsed

    def raw(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()


ACTIONS = {"actions": {
    "echo_hi": {"argv": ["echo", "hi"], "description": "say hi", "confirm": True},
    "echo_free": {"argv": ["echo", "free"], "confirm": False},
}}


class CorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(actions_json=None)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_health_no_origin(self):
        st, h, b = self.fx.request("GET", "/v1/health")
        self.assertEqual(st, 200)
        self.assertTrue(b["ok"])
        self.assertFalse(b["actions_enabled"])
        self.assertIn("gpu", b["probes"])
        self.assertNotIn("access-control-allow-origin", h)
        self.assertNotIn("server", h)
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["content-type"], "application/json; charset=utf-8")

    def test_null_origin_reflected(self):
        st, h, _ = self.fx.request("GET", "/v1/probes", {"Origin": "null"})
        self.assertEqual(st, 200)
        self.assertEqual(h["access-control-allow-origin"], "null")
        self.assertEqual(h["vary"], "Origin")

    def test_loopback_origins(self):
        for o in ["http://127.0.0.1:8181", "http://localhost", "http://localhost:3000", "http://[::1]:8000", "https://allowed.example"]:
            st, h, _ = self.fx.request("GET", "/v1/health", {"Origin": o})
            self.assertEqual(st, 200, o)
            self.assertEqual(h["access-control-allow-origin"], o)

    def test_bad_origin_403_without_cors(self):
        for o in ["https://evil.example", "http://127.0.0.1.evil.example", "http://localhost.evil", "https://127.0.0.1:8181"]:
            st, h, _ = self.fx.request("GET", "/v1/probe/gpu", {"Origin": o})
            self.assertEqual(st, 403, o)
            self.assertNotIn("access-control-allow-origin", h, o)

    def test_preflight(self):
        st, h, _ = self.fx.request("OPTIONS", "/v1/probe/gpu", {"Origin": "null", "Access-Control-Request-Method": "GET"})
        self.assertEqual(st, 204)
        self.assertEqual(h["access-control-allow-origin"], "null")
        self.assertIn("X-Bridge-Token", h["access-control-allow-headers"])
        st, h, _ = self.fx.request("OPTIONS", "/v1/probe/gpu", {"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)

    def test_bad_names(self):
        st, _, b = self.fx.request("GET", "/v1/probe/../etc/passwd")
        self.assertIn(st, (404,))
        st, _, b = self.fx.request("GET", "/v1/probe/NoSuch")
        self.assertEqual(st, 404)
        st, _, b = self.fx.request("GET", "/v1/all?names=cpu,Bad-Name,nothere")
        self.assertEqual(st, 200)  # /v1/all is never broken by one bad name
        self.assertTrue(b["results"]["cpu"]["ok"] or b["results"]["cpu"]["error"])
        self.assertEqual(b["results"]["Bad-Name"]["error"]["type"], "BadName")
        self.assertEqual(b["results"]["nothere"]["error"]["type"], "UnknownProbe")

    def test_actions_disabled_without_file(self):
        st, _, b = self.fx.request("GET", "/v1/actions")
        self.assertEqual(st, 404)
        self.assertEqual(b["error"]["type"], "ActionsDisabled")
        st, _, _ = self.fx.request("POST", "/v1/action/echo_hi", {"X-Bridge-Token": self.fx.srv.token}, b'{"confirm":true}')
        self.assertEqual(st, 404)

    def test_stream_one_event(self):
        c = http.client.HTTPConnection("127.0.0.1", self.fx.port, timeout=5)
        c.request("GET", "/v1/stream?names=cpu&interval_ms=100", headers={"Origin": "null"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "null")
        line = r.fp.readline()
        self.assertEqual(line, b"event: probes\n")
        data = r.fp.readline()
        self.assertTrue(data.startswith(b"data: {"))
        body = json.loads(data[6:])
        self.assertIn("cpu", body["results"])
        c.close()


class ActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(actions_json=ACTIONS)
        cls.tok = cls.fx.srv.token

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_token_file_mode(self):
        self.assertEqual(oct(os.stat(self.fx.token_file).st_mode & 0o777), "0o600")
        self.assertGreaterEqual(len(self.tok), 32)

    def test_catalogue(self):
        st, _, b = self.fx.request("GET", "/v1/actions", {"Origin": "null"})
        self.assertEqual(st, 200)
        self.assertEqual([a["name"] for a in b], ["echo_free", "echo_hi"])
        self.assertTrue(b[1]["confirm"])
        st, _, b = self.fx.request("GET", "/v1/health")
        self.assertTrue(b["actions_enabled"])

    def test_missing_token_401(self):
        st, _, b = self.fx.request("POST", "/v1/action/echo_hi", {"Origin": "null"}, b'{"confirm":true}')
        self.assertEqual(st, 401)
        st, _, b = self.fx.request("POST", "/v1/action/echo_hi", {"X-Bridge-Token": "wrong" * 8}, b'{"confirm":true}')
        self.assertEqual(st, 401)

    def test_bad_origin_403_even_with_token(self):
        st, h, _ = self.fx.request("POST", "/v1/action/echo_hi", {"Origin": "https://evil.example", "X-Bridge-Token": self.tok}, b'{"confirm":true}')
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)

    def test_confirm_required(self):
        st, _, b = self.fx.request("POST", "/v1/action/echo_hi", {"X-Bridge-Token": self.tok}, b"{}")
        self.assertEqual(st, 400)
        self.assertEqual(b["error"]["type"], "ConfirmRequired")
        st, _, b = self.fx.request("POST", "/v1/action/echo_hi", {"X-Bridge-Token": self.tok}, b'{"confirm":"yes"}')
        self.assertEqual(st, 400)

    def test_runs_with_token_and_confirm(self):
        st, _, b = self.fx.request("POST", "/v1/action/echo_hi", {"X-Bridge-Token": self.tok, "Origin": "null"}, b'{"confirm":true}')
        self.assertEqual(st, 200)
        self.assertTrue(b["ok"])
        self.assertEqual(b["exit_code"], 0)
        self.assertEqual(b["stdout"], "hi\n")
        self.assertEqual(b["name"], "echo_hi")

    def test_no_confirm_action_runs_without_body(self):
        st, _, b = self.fx.request("POST", "/v1/action/echo_free", {"X-Bridge-Token": self.tok})
        self.assertEqual(st, 200)
        self.assertEqual(b["stdout"], "free\n")

    def test_argv_never_built_from_request(self):
        # Extra body fields and query strings are ignored; argv comes from the file only.
        st, _, b = self.fx.request("POST", "/v1/action/echo_hi?arg=pwned", {"X-Bridge-Token": self.tok},
                                   b'{"confirm":true,"argv":["rm","-rf","/"],"args":["pwned"]}')
        self.assertEqual(st, 200)
        self.assertEqual(b["stdout"], "hi\n")

    def test_unknown_action_404(self):
        st, _, b = self.fx.request("POST", "/v1/action/nope", {"X-Bridge-Token": self.tok}, b'{"confirm":true}')
        self.assertEqual(st, 404)
        self.assertEqual(b["error"]["type"], "UnknownAction")

    def test_invalid_file_rejected(self):
        with open(self.fx.actions_path, "w") as f:
            json.dump({"actions": {"bad name!": {"argv": ["echo"]}}}, f)
        os.utime(self.fx.actions_path, (1, 1))  # force a different mtime
        st, _, b = self.fx.request("GET", "/v1/actions")
        self.assertEqual(st, 500)
        self.assertEqual(b["error"]["type"], "ActionsInvalid")
        with open(self.fx.actions_path, "w") as f:
            json.dump(ACTIONS, f)


APP = "<!doctype html><title>Llama Manager</title><meta name=description content='Herd them'><meta name=app-icon content='🦙'><body>v1"


class AppsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(actions_json=None)
        cls.tok = cls.fx.srv.token
        cls.H = {"X-Bridge-Token": cls.tok, "Origin": "http://localhost"}

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_launcher_served_at_root(self):
        st, h, data = self.fx.raw("GET", "/")
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/html"))
        self.assertIn(b"<title>", data)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertNotIn("server", h)

    def test_builtin_app_static(self):
        st, h, _ = self.fx.raw("GET", "/apps/dash")
        self.assertEqual(st, 301)
        self.assertEqual(h["location"], "/apps/dash/")
        st, h, data = self.fx.raw("GET", "/apps/dash/")
        self.assertEqual(st, 200)
        self.assertIn(b"Llama Dashboard", data)
        st, h, data = self.fx.raw("GET", "/apps/dash/app.js")
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/javascript"))
        for bad in ["/apps/dash/../../etc/passwd", "/apps/dash/%2e%2e/%2e%2e/etc/passwd", "/apps/nope/", "/apps/Bad%20Slug/"]:
            st, _, _ = self.fx.raw("GET", bad)
            self.assertEqual(st, 404, bad)

    def test_list_has_builtin(self):
        st, _, b = self.fx.request("GET", "/v1/apps", {"Origin": "null"})
        self.assertEqual(st, 200)
        self.assertEqual(b[0]["slug"], "dash")
        self.assertTrue(b[0]["builtin"])
        self.assertEqual(b[0]["icon"], "🦙")

    def test_upload_requires_token_and_good_origin(self):
        st, _, b = self.fx.request("POST", "/v1/apps", {"Content-Type": "text/html", "Origin": "null"}, APP.encode())
        self.assertEqual(st, 401)
        st, h, _ = self.fx.request("POST", "/v1/apps", {"Content-Type": "text/html", "Origin": "https://evil.example", "X-Bridge-Token": self.tok}, APP.encode())
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)
        self.assertNotIn("llama-manager", [a["slug"] for a in self.fx.request("GET", "/v1/apps")[2]])  # nothing installed

    def test_upload_open_replace_uninstall(self):
        st, _, b = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "text/html", "X-Filename": "llama.html"}, APP.encode())
        self.assertEqual(st, 201, b)
        self.assertEqual(b["app"]["slug"], "llama-manager")
        self.assertEqual(b["app"]["icon"], "🦙")
        self.assertEqual(b["app"]["original_filename"], "llama.html")
        st, h, data = self.fx.raw("GET", "/apps/llama-manager/")
        self.assertEqual(st, 200)
        self.assertIn(b"v1", data)
        st, _, b = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "text/html"}, APP.replace("v1", "v2").encode())
        self.assertEqual(st, 201)
        self.assertTrue(b["app"]["replaced"].startswith("llama-manager-"))
        self.assertIn(b"v2", self.fx.raw("GET", "/apps/llama-manager/")[2])
        # uninstall: confirm required, builtin protected, then trashed
        st, _, b = self.fx.request("DELETE", "/v1/apps/llama-manager", self.H, b"{}")
        self.assertEqual(st, 400)
        self.assertEqual(b["error"]["type"], "ConfirmRequired")
        st, _, b = self.fx.request("DELETE", "/v1/apps/dash", self.H, b'{"confirm":true}')
        self.assertEqual(st, 403)
        st, _, _ = self.fx.request("DELETE", "/v1/apps/llama-manager", {"Origin": "null"}, b'{"confirm":true}')
        self.assertEqual(st, 401)
        st, _, b = self.fx.request("DELETE", "/v1/apps/llama-manager", self.H, b'{"confirm":true}')
        self.assertEqual(st, 200)
        self.assertTrue(b["trashed"].startswith("llama-manager-"))
        self.assertEqual(self.fx.raw("GET", "/apps/llama-manager/")[0], 404)
        self.assertEqual(len([d for d in os.listdir(os.path.join(self.fx.apps_root, ".trash")) if d.startswith("llama-manager-")]), 2)

    def test_link_app_redirects(self):
        st, _, b = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "application/json"},
                                   json.dumps({"kind": "link", "url": "http://127.0.0.1:8080/", "title": "Router UI", "icon": "🧭"}).encode())
        self.assertEqual(st, 201, b)
        st, h, _ = self.fx.raw("GET", "/apps/router-ui/")
        self.assertEqual(st, 302)
        self.assertEqual(h["location"], "http://127.0.0.1:8080/")
        st, _, b = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "application/json"}, json.dumps({"url": "javascript:alert(1)", "title": "x"}).encode())
        self.assertEqual(st, 400)
        self.fx.request("DELETE", "/v1/apps/router-ui", self.H, b'{"confirm":true}')

    def test_bad_uploads(self):
        st, _, b = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "text/html"}, b"no tags at all")
        self.assertEqual(st, 400)
        st, _, b = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "image/png"}, b"\x89PNG<x>")
        self.assertEqual(st, 415)
        st, _, _ = self.fx.request("POST", "/v1/apps", {**self.H, "Content-Type": "text/html"}, b"<title>dash</title>")
        self.assertEqual(st, 409)


if __name__ == "__main__":
    unittest.main()
