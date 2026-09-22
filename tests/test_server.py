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
        cfg = Config(bind="127.0.0.1", port=0, extra_origins=["https://allowed.example"],
                     actions_file=self.actions_path, token_file=self.token_file)
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


if __name__ == "__main__":
    unittest.main()
