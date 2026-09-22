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
                     sensitive_token_file=os.path.join(self.tmp.name, "rt", "token-sensitive"),
                     apps_root=self.apps_root, builtins={"dash": self.builtin_dir},
                     fs_roots=[self.tmp.name], settings_file=os.path.join(self.tmp.name, "settings.json"))
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
        for o in ["http://127.0.0.1:8181", "http://localhost", "http://localhost:3000", "http://[::1]:8000", "https://allowed.example",
                  "http://llama-dash.localhost", "http://wtop.localhost:8182"]:
            st, h, _ = self.fx.request("GET", "/v1/health", {"Origin": o})
            self.assertEqual(st, 200, o)
            self.assertEqual(h["access-control-allow-origin"], o)

    def test_bad_origin_403_without_cors(self):
        for o in ["https://evil.example", "http://127.0.0.1.evil.example", "http://localhost.evil", "https://127.0.0.1:8181",
                  "http://evil.localhost.example", "https://wtop.localhost", "http://Wtop.localhost", "http://a.b.localhost"]:
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


class PerAppOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(actions_json=None)
        cls.tok = cls.fx.srv.token

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_app_host_serves_the_app(self):
        st, h, data = self.fx.raw("GET", "/", {"Host": "dash.localhost"})
        self.assertEqual(st, 200)
        self.assertIn(b"Llama Dashboard", data)
        self.assertIn(b"data-sysbridge-home", data)
        self.assertIn(b'"http://localhost/"', data)                       # absolute launcher link, port 80 implied
        st, h, data = self.fx.raw("GET", "/", {"Host": "dash.localhost:8182"})
        self.assertIn(b'"http://localhost:8182/"', data)                   # dev port carried through
        st, h, data = self.fx.raw("GET", "/app.js", {"Host": "dash.localhost"})
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/javascript"))
        self.assertNotIn(b"data-sysbridge-home", data)

    def test_token_injected_only_into_browser_documents(self):
        tok = self.fx.srv.token.encode()
        browser = {"Host": "dash.localhost", "Sec-Fetch-Dest": "document", "Accept": "text/html,*/*"}
        st, _, data = self.fx.raw("GET", "/", browser)
        self.assertIn(b"data-sysbridge-token", data)
        self.assertIn(tok, data)
        self.assertLess(data.index(b"data-sysbridge-token"), data.index(b"Llama Dashboard"))   # before the app's own content
        self.assertNotIn(self.fx.srv.sensitive_token.encode(), data)                            # never the sensitive one
        # the launcher document gets it too
        st, _, data = self.fx.raw("GET", "/", {"Host": "localhost", "Sec-Fetch-Dest": "document"})
        self.assertIn(tok, data)
        # old browsers without Sec-Fetch: Accept decides
        st, _, data = self.fx.raw("GET", "/", {"Host": "dash.localhost", "Accept": "text/html"})
        self.assertIn(tok, data)
        # curl-like requests do not get it
        st, _, data = self.fx.raw("GET", "/", {"Host": "dash.localhost"})
        self.assertNotIn(tok, data)
        st, _, data = self.fx.raw("GET", "/", {"Host": "dash.localhost", "Accept": "*/*"})
        self.assertNotIn(tok, data)
        # fetch()/XHR from a page is not a document either
        st, _, data = self.fx.raw("GET", "/", {"Host": "dash.localhost", "Sec-Fetch-Dest": "empty", "Accept": "*/*"})
        self.assertNotIn(tok, data)
        # never into non-HTML or API responses
        st, _, data = self.fx.raw("GET", "/app.js", browser)
        self.assertNotIn(tok, data)
        st, _, data = self.fx.raw("GET", "/v1/health", browser)
        self.assertNotIn(tok, data)
        # content-length matches the modified body
        st, h, data = self.fx.raw("GET", "/", browser)
        self.assertEqual(int(h["content-length"]), len(data))

    def test_home_position_setting(self):
        H = {"X-Bridge-Token": self.fx.srv.token, "Origin": "null", "Content-Type": "application/json"}
        st, _, b = self.fx.request("GET", "/v1/settings", {"Origin": "null"})
        self.assertEqual(st, 200)
        self.assertEqual(b["settings"]["home_position"], "top")
        self.assertIn("home_position", b["allowed"])
        self.assertIn(b'"top"', self.fx.raw("GET", "/", {"Host": "dash.localhost"})[2])
        st, _, _ = self.fx.request("PUT", "/v1/settings", {"Origin": "null", "Content-Type": "application/json"}, b'{"home_position":"left"}')
        self.assertEqual(st, 401)
        st, _, b = self.fx.request("PUT", "/v1/settings", H, b'{"home_position":"diagonal"}')
        self.assertEqual(st, 400)
        st, _, b = self.fx.request("PUT", "/v1/settings", H, b'{"home_position":"left"}')
        self.assertEqual(st, 200)
        self.assertEqual(b["settings"]["home_position"], "left")
        data = self.fx.raw("GET", "/", {"Host": "dash.localhost"})[2]
        self.assertIn(b'POS="left"', data)
        self.assertNotIn(b'POS="top"', data)
        self.fx.request("PUT", "/v1/settings", H, b'{"home_position":"top"}')

    def test_token_injection_can_be_disabled(self):
        self.fx.srv.cfg.inject_token = False
        try:
            st, _, data = self.fx.raw("GET", "/", {"Host": "dash.localhost", "Sec-Fetch-Dest": "document"})
            self.assertNotIn(self.fx.srv.token.encode(), data)
            self.assertIn(b"data-sysbridge-home", data)
        finally:
            self.fx.srv.cfg.inject_token = True

    def test_doctype_kept_first(self):
        from bridge.server import with_token
        out = with_token(b"<!DOCTYPE html><html><head></head></html>", "T")
        self.assertTrue(out.startswith(b"<!DOCTYPE html>\n<script data-sysbridge-token>"))
        out = with_token(b"<meta charset=utf-8><title>x</title>", "T")
        self.assertTrue(out.startswith(b"<script data-sysbridge-token>"))

    def test_app_host_api_and_launcher_paths_still_work(self):
        st, _, b = self.fx.request("GET", "/v1/health", {"Host": "dash.localhost"})
        self.assertEqual(st, 200)
        self.assertTrue(b["ok"])
        st, _, data = self.fx.raw("GET", "/apps/dash/", {"Host": "dash.localhost"})
        self.assertEqual(st, 200)
        st, _, data = self.fx.raw("GET", "/", {"Host": "localhost"})
        self.assertNotIn(b"data-sysbridge-home", data)                    # launcher is never injected
        self.assertIn(b"<title>sysbridge</title>", data)
        st, _, data = self.fx.raw("GET", "/", {"Host": "127.0.0.1:8182"})
        self.assertIn(b"<title>sysbridge</title>", data)

    def test_unknown_app_host_and_traversal(self):
        st, _, b = self.fx.request("GET", "/", {"Host": "nosuch.localhost"})
        self.assertEqual(st, 404)
        self.assertIn("http://localhost/", b["error"]["message"])
        for bad in ["/../../etc/passwd", "/%2e%2e/etc/passwd", "/manifest.json/../../../etc/passwd"]:
            st, _, _ = self.fx.raw("GET", bad, {"Host": "dash.localhost"})
            self.assertEqual(st, 404, bad)

    def test_link_app_host_redirects(self):
        H = {"X-Bridge-Token": self.tok, "Origin": "http://localhost", "Content-Type": "application/json"}
        self.fx.request("POST", "/v1/apps", H, json.dumps({"kind": "link", "url": "http://127.0.0.1:8080/", "title": "Router UI"}).encode())
        st, h, _ = self.fx.raw("GET", "/", {"Host": "router-ui.localhost"})
        self.assertEqual(st, 302)
        self.assertEqual(h["location"], "http://127.0.0.1:8080/")
        self.fx.request("DELETE", "/v1/apps/router-ui", H, b'{"confirm":true}')

    def test_two_token_classes(self):
        self.assertNotEqual(self.fx.srv.token, self.fx.srv.sensitive_token)
        p = os.path.join(self.fx.tmp.name, "rt", "token-sensitive")
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        st, _, _ = self.fx.request("POST", "/v1/apps", {"X-Bridge-Sensitive-Token": self.fx.srv.sensitive_token, "Content-Type": "text/html"}, b"<title>x</title>")
        self.assertEqual(st, 401)                                          # the sensitive token is not a super-token


class FsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(actions_json=None)
        cls.S = {"X-Bridge-Sensitive-Token": cls.fx.srv.sensitive_token, "Origin": "http://web-file.localhost"}

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_token_rules_default_and_strict(self):
        st, _, _ = self.fx.request("GET", "/v1/fs/roots", {"Origin": "null"})
        self.assertEqual(st, 401)                                                   # no token at all
        st, _, _ = self.fx.request("GET", "/v1/fs/roots", {"X-Bridge-Token": self.fx.srv.token})
        self.assertEqual(st, 200)                                                   # default: the ordinary (injected) token opens it
        st, _, _ = self.fx.request("GET", "/v1/fs/roots", self.S)
        self.assertEqual(st, 200)                                                   # the sensitive one always works
        st, h, _ = self.fx.request("GET", "/v1/fs/roots", {**self.S, "Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)
        self.fx.srv.cfg.fs_strict = True
        try:
            st, _, b = self.fx.request("GET", "/v1/fs/roots", {"X-Bridge-Token": self.fx.srv.token})
            self.assertEqual(st, 401)                                               # strict: ordinary token refused
            self.assertIn("strict", b["error"]["message"])
            st, _, _ = self.fx.request("GET", "/v1/fs/roots", self.S)
            self.assertEqual(st, 200)
        finally:
            self.fx.srv.cfg.fs_strict = False

    def test_roots_ls_read_download(self):
        st, _, b = self.fx.request("GET", "/v1/fs/roots", self.S)
        self.assertEqual(st, 200)
        root = b["roots"][0]["path"]
        st, _, b = self.fx.request("GET", "/v1/fs/ls?path=" + root, self.S)
        self.assertEqual(st, 200)
        self.assertIn("rt", [e["name"] for e in b["entries"]])
        st, _, b = self.fx.request("GET", "/v1/fs/ls?path=/etc", self.S)
        self.assertEqual(st, 403)
        st, _, b = self.fx.request("GET", "/v1/fs/read?path=" + os.path.join(root, "actions.json"), self.S)
        self.assertIn(st, (200, 404))
        st, h, data = self.fx.raw("GET", "/v1/fs/download?path=" + os.path.join(root, "rt", "token"), self.S)
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "application/octet-stream")
        self.assertIn("attachment", h["content-disposition"])
        self.assertEqual(int(h["content-length"]), len(data))
        st, _, _ = self.fx.request("GET", "/v1/fs/nope?path=/", self.S)
        self.assertEqual(st, 404)


class LlamaApiTests(unittest.TestCase):
    """Gating only — the upstream logic is covered in test_llama with a fake server."""
    @classmethod
    def setUpClass(cls):
        cls.fx = ServerFixture(actions_json=None)
        cls.tok = cls.fx.srv.token
        cls.H = {"X-Bridge-Token": cls.tok, "Origin": "null", "Content-Type": "application/json"}

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_probes_listed(self):
        st, _, b = self.fx.request("GET", "/v1/health")
        self.assertIn("llama", b["probes"])
        self.assertIn("llama_slots", b["probes"])

    def test_gating(self):
        st, _, b = self.fx.request("POST", "/v1/llama/router/load", {"Origin": "null", "Content-Type": "application/json"}, b'{"model":"x","confirm":true}')
        self.assertEqual(st, 401)
        st, h, _ = self.fx.request("POST", "/v1/llama/router/load", {**self.H, "Origin": "https://evil.example"}, b'{"model":"x","confirm":true}')
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)
        st, _, b = self.fx.request("POST", "/v1/llama/router/load", self.H, b'{"model":"x"}')
        self.assertEqual(st, 400)
        self.assertEqual(b["error"]["type"], "ConfirmRequired")
        st, _, b = self.fx.request("POST", "/v1/llama/nosuch/load", self.H, b'{"model":"x","confirm":true}')
        self.assertEqual(st, 404)
        st, _, b = self.fx.request("POST", "/v1/llama/router/delete", self.H, b'{"model":"x","confirm":true}')
        self.assertEqual(st, 404)

    def test_chat_gating(self):
        # no token needed, but the body is validated before anything is contacted, and bad servers 404
        st, _, b = self.fx.request("POST", "/v1/llama/router/chat", {"Origin": "null", "Content-Type": "application/json"}, b'{"model":"x"}')
        self.assertEqual(st, 400)
        st, _, b = self.fx.request("POST", "/v1/llama/nosuch/chat", {"Origin": "null", "Content-Type": "application/json"}, b'{"model":"x","messages":[{"role":"user","content":"hi"}]}')
        self.assertEqual(st, 404)
        st, h, _ = self.fx.request("POST", "/v1/llama/router/chat", {"Origin": "https://evil.example", "Content-Type": "application/json"}, b'{"model":"x","messages":[{"role":"user","content":"hi"}]}')
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)


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
        self.assertIn(b"data-sysbridge-home", data)          # home button injected into app pages
        self.assertEqual(int(h["content-length"]), len(data))
        self.assertNotIn(b"data-sysbridge-home", self.fx.raw("GET", "/")[2])            # not into the launcher
        self.assertNotIn(b"data-sysbridge-home", self.fx.raw("GET", "/apps/dash/app.js")[2])  # not into non-HTML
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
