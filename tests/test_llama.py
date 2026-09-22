"""llama.py against a fake llama-server on an ephemeral port."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

from bridge import llama

ROUTER_MODELS = {"data": [
    {"id": "big", "status": {"value": "loaded", "args": [], "preset": "[big]\nmodel = /m/big.gguf\n"}, "meta": {"size": 10, "n_params": 5, "n_ctx": 8}},
    {"id": "small", "status": {"value": "unloaded", "args": [], "preset": "[small]\nmodel = /m/small.gguf\n"}},
]}
SINGLE_MODELS = {"models": [{"name": "solo"}], "data": [{"id": "solo", "meta": {"size": 1}}]}


class Fake(BaseHTTPRequestHandler):
    mode = "router"
    seen = []

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        Fake.seen.append(("GET", u.path, q))
        if u.path == "/health":
            return self._json(200, {"status": "ok"})
        if u.path == "/models":
            return self._json(200, ROUTER_MODELS if Fake.mode == "router" else SINGLE_MODELS)
        if u.path == "/props":
            if Fake.mode == "router" and q.get("autoload") != ["false"]:
                return self._json(500, {"error": {"message": "TEST: autoload not disabled"}})
            return self._json(200, {"model_alias": q.get("model", ["solo"])[0], "total_slots": 1, "build_info": "fake", "chat_template": "x" * 5000})
        if u.path == "/slots":
            if Fake.mode == "router" and q.get("model") == ["small"]:
                return self._json(400, {"error": {"message": "model is not loaded"}})
            return self._json(200, [{"id": 0, "is_processing": True, "n_prompt_tokens": 7}])
        self._json(404, {"error": {"message": "nope"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Fake.seen.append(("POST", self.path, body))
        if self.path in ("/models/load", "/models/unload"):
            return self._json(200, {"success": True})
        if self.path == "/v1/chat/completions":
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for tok in ["Hel", "lo"]:
                    self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": tok}}]}) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": "Hello"}}]})
        self._json(404, {"error": {"message": "nope"}})


class LlamaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        cls.saved = dict(llama.SERVERS)
        llama.SERVERS.clear()
        llama.SERVERS.update({"router": cls.url, "dead": "http://127.0.0.1:1"})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        llama.SERVERS.clear()
        llama.SERVERS.update(cls.saved)

    def setUp(self):
        Fake.mode = "router"
        Fake.seen.clear()

    def test_parse_servers(self):
        self.assertEqual(llama.parse_servers(None), {"router": "http://127.0.0.1:8080"})
        self.assertEqual(llama.parse_servers("a=http://x:1/, b = https://y "), {"a": "http://x:1", "b": "https://y"})
        for bad in ["Router=http://x", "a=ftp://x", "a", "a=x"]:
            with self.assertRaises(ValueError):
                llama.parse_servers(bad)

    def test_infer_mode(self):
        self.assertEqual(llama.infer_mode(ROUTER_MODELS), "router")
        self.assertEqual(llama.infer_mode(SINGLE_MODELS), "single")
        self.assertEqual(llama.infer_mode({}), "unknown")
        self.assertEqual(llama.infer_mode(None), "unknown")

    def test_fetch_router_uses_autoload_false_everywhere(self):
        sv = llama.fetch_server("router", self.url)
        self.assertTrue(sv["ok"])
        self.assertEqual(sv["mode"], "router")
        self.assertEqual([m["id"] for m in sv["models"]], ["big", "small"])
        self.assertEqual(sv["props"]["model_alias"], "big")
        self.assertNotIn("chat_template", sv["props"])
        for method, path, q in Fake.seen:
            if method == "GET":
                self.assertEqual(q.get("autoload"), ["false"], (path, q))

    def test_fetch_single_and_dead(self):
        Fake.mode = "single"
        sv = llama.fetch_server("router", self.url)
        self.assertEqual(sv["mode"], "single")
        self.assertEqual(sv["props"]["model_alias"], "solo")
        dead = llama.fetch_server("dead", "http://127.0.0.1:1")
        self.assertFalse(dead["ok"])
        self.assertIn("unreachable", dead["error"])

    def test_probes_via_registry(self):
        from bridge.registry import Registry
        reg = Registry()
        llama.register(reg)
        env = reg.get("llama")
        self.assertTrue(env["ok"])
        self.assertTrue(env["data"]["servers"]["router"]["ok"])
        self.assertFalse(env["data"]["servers"]["dead"]["ok"])
        slots = reg.get("llama_slots")["data"]["servers"]
        self.assertEqual(list(slots["router"]["models"]), ["big"])          # only the loaded model
        self.assertTrue(slots["router"]["models"]["big"]["slots"][0]["is_processing"])
        self.assertEqual(slots["dead"], {"ok": False, "models": {}})

    def test_load_unload_validation(self):
        with self.assertRaises(llama.LlamaError) as cm:
            llama.load_unload("nope", "load", "big")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(llama.LlamaError) as cm:
            llama.load_unload("router", "delete", "big")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(llama.LlamaError) as cm:
            llama.load_unload("router", "load", "not-a-model")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(llama.LlamaError) as cm:
            llama.load_unload("dead", "load", "big")
        self.assertEqual(cm.exception.status, 502)
        Fake.mode = "single"
        with self.assertRaises(llama.LlamaError) as cm:
            llama.load_unload("router", "unload", "solo")
        self.assertEqual(cm.exception.status, 409)
        self.assertFalse([s for s in Fake.seen if s[0] == "POST"])          # nothing reached the upstream

    def test_load_ok(self):
        r = llama.load_unload("router", "load", "small")
        self.assertTrue(r["ok"])
        self.assertEqual([s for s in Fake.seen if s[0] == "POST"], [("POST", "/models/load", {"model": "small"})])


class ChatTests(LlamaTests):
    def test_payload_whitelist(self):
        p = llama.chat_payload({"model": "big", "messages": [{"role": "user", "content": "hi", "extra": 1}], "temperature": 0.2,
                                "max_tokens": 10, "n_probs": 5, "grammar": "root ::= x", "stream": False, "stop": ["\n"]})
        self.assertEqual(p, {"model": "big", "messages": [{"role": "user", "content": "hi"}], "stream": False, "temperature": 0.2, "max_tokens": 10, "stop": ["\n"]})
        for bad in [{}, {"model": "big"}, {"model": "big", "messages": []}, {"model": "big", "messages": [{"role": "tool", "content": "x"}]},
                    {"model": "big", "messages": [{"role": "user", "content": ["parts"]}]}, "nope"]:
            with self.assertRaises(llama.LlamaError):
                llama.chat_payload(bad)

    def test_chat_only_loaded_models(self):
        with self.assertRaises(llama.LlamaError) as cm:
            llama.chat_open("router", llama.chat_payload({"model": "small", "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual(cm.exception.status, 409)
        self.assertFalse([s for s in Fake.seen if s[0] == "POST"])
        status, ctype, resp = llama.chat_open("router", llama.chat_payload({"model": "big", "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/event-stream"))
        body = resp.read().decode()
        resp.close()
        self.assertIn("Hel", body)
        self.assertIn("[DONE]", body)
        sent = [s for s in Fake.seen if s[0] == "POST"][-1][2]
        self.assertEqual(sent["model"], "big")
        self.assertTrue(sent["stream"])
        with self.assertRaises(llama.LlamaError) as cm:
            llama.chat_open("dead", llama.chat_payload({"model": "big", "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual(cm.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
