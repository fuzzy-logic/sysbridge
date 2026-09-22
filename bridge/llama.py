"""llama-server upstreams as probes, plus the one write the dashboard needs.

The dashboard is an ordinary app: served from /apps/llama-dash/ and allowed to
talk only to its own origin. So the bridge does the talking to the llama
servers and exposes the result as probes:

    llama        servers → health, mode, props, models        ttl 2 s, async
    llama_slots  servers → per-model /slots (who is generating) ttl 1 s, async

and one narrow write, wired in server.py:

    POST /v1/llama/<server>/load|unload  {"model": id, "confirm": true}

Upstreams come from SYSBRIDGE_LLAMA_SERVERS, "name=url,name=url"; default
"router=http://127.0.0.1:8080,reviewer=http://127.0.0.1:8127". Names match
^[a-z0-9_-]{1,32}$ and become URL segments.

Footguns encoded here (verified 2026-09-22, EngramHalo.cpp build b1-c26c2ea):
every router GET carries autoload=false because GET /props?model=<unloaded>
loads that model; a single-model server ignores the flag. Under --models-max 1
a load evicts the resident model — the caller states that in its confirm.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .registry import Registry

NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
DEFAULT_SERVERS = "router=http://127.0.0.1:8080,reviewer=http://127.0.0.1:8127"
TIMEOUT_S = 2.5


class LlamaError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_servers(spec: Optional[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (spec or DEFAULT_SERVERS).split(","):
        part = part.strip()
        if not part:
            continue
        name, _, url = part.partition("=")
        name, url = name.strip(), url.strip().rstrip("/")
        if not NAME_RE.match(name) or not url.startswith(("http://", "https://")):
            raise ValueError(f"bad SYSBRIDGE_LLAMA_SERVERS entry {part!r}; want name=http://host:port")
        out[name] = url
    return out


SERVERS: dict[str, str] = parse_servers(os.environ.get("SYSBRIDGE_LLAMA_SERVERS"))


def _get(url: str, params: Optional[dict] = None, timeout: float = TIMEOUT_S):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — loopback URLs from config
        return json.loads(r.read(4 << 20).decode("utf-8", "replace") or "null")


def _post(url: str, body: dict, timeout: float = 20.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read(1 << 20).decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "null")
        except ValueError:
            body = None
        return e.code, body


def infer_mode(models: object) -> str:
    """Router: data[].status.value exists. Single-model: {models:[ollama-stub], data:[…]} without status."""
    data = models.get("data") if isinstance(models, dict) else None
    if isinstance(data, list) and any(isinstance(m, dict) and isinstance(m.get("status"), dict) for m in data):
        return "router"
    if isinstance(models, dict) and (isinstance(models.get("models"), list) or data):
        return "single"
    return "unknown"


def _err(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"unreachable ({e.reason})"
    return f"{type(e).__name__}: {e}"


def fetch_server(name: str, url: str) -> dict:
    """One server → {ok, url, mode, health, props, models, error}. Never raises."""
    out: dict = {"name": name, "url": url, "ok": False, "mode": "unknown", "health": None, "props": None, "models": [], "error": None}
    try:
        h = _get(url + "/health", {"autoload": "false"})
        out["health"] = h
        out["ok"] = isinstance(h, dict) and h.get("status") == "ok"
    except Exception as e:  # noqa: BLE001
        out["error"] = _err(e)
        return out
    try:
        m = _get(url + "/models", {"autoload": "false"})
        out["mode"] = infer_mode(m)
        out["models"] = [x for x in (m.get("data") or []) if isinstance(x, dict)] if isinstance(m, dict) else []
    except Exception as e:  # noqa: BLE001
        out["error"] = f"/models: {_err(e)}"
    try:
        if out["mode"] == "router":
            loaded = next((x for x in out["models"] if (x.get("status") or {}).get("value") == "loaded"), None)
            if loaded:
                out["props"] = _get(url + "/props", {"model": loaded["id"], "autoload": "false"})
        else:
            out["props"] = _get(url + "/props")
    except Exception as e:  # noqa: BLE001
        out["props_error"] = _err(e)
    if isinstance(out["props"], dict):
        out["props"].pop("chat_template", None)          # multi-KB, never displayed
        out["props"].pop("default_generation_settings", None)
    return out


def loaded_ids(server: dict) -> list[str]:
    if server.get("mode") == "router":
        return [m["id"] for m in server.get("models", []) if (m.get("status") or {}).get("value") == "loaded" and m.get("id")]
    return [m["id"] for m in server.get("models", []) if m.get("id")] or ["default"]


def register(reg: Registry) -> None:
    @reg.probe("llama", ttl_ms=2000, description="llama-server upstreams: health, mode, props, models (every router GET carries autoload=false)", async_refresh=True)
    def llama() -> dict:
        return {"servers": {name: fetch_server(name, url) for name, url in SERVERS.items()}}

    @reg.probe("llama_slots", ttl_ms=1000, description="llama-server /slots per loaded model: who is generating right now", async_refresh=True)
    def llama_slots() -> dict:
        out: dict = {"servers": {}}
        cur = reg.get("llama")
        servers = (cur.get("data") or {}).get("servers", {}) if cur.get("data") else {}
        for name, url in SERVERS.items():
            sv = servers.get(name) or {}
            entry: dict = {"ok": bool(sv.get("ok")), "models": {}}
            if not sv.get("ok"):
                out["servers"][name] = entry
                continue
            if sv.get("mode") == "router":
                for mid in loaded_ids(sv):
                    try:
                        rows = _get(url + "/slots", {"model": mid, "autoload": "false"})
                        entry["models"][mid] = {"slots": rows if isinstance(rows, list) else [], "error": None}
                    except Exception as e:  # noqa: BLE001
                        entry["models"][mid] = {"slots": [], "error": _err(e)}
            else:
                label = ((sv.get("props") or {}).get("model_alias")) or (loaded_ids(sv) or ["model"])[0]
                try:
                    rows = _get(url + "/slots")
                    entry["models"][label] = {"slots": rows if isinstance(rows, list) else [], "error": None}
                except Exception as e:  # noqa: BLE001
                    entry["models"][label] = {"slots": [], "error": _err(e)}
            out["servers"][name] = entry
        return out


def load_unload(server: str, action: str, model: str) -> dict:
    """POST /models/load|unload to a named upstream. The model must be one the server lists."""
    if server not in SERVERS:
        raise LlamaError(f"no llama server named {server!r}", 404)
    if action not in ("load", "unload"):
        raise LlamaError("action must be load or unload", 404)
    if not isinstance(model, str) or not (1 <= len(model) <= 128):
        raise LlamaError("model must be a non-empty string")
    url = SERVERS[server]
    try:
        m = _get(url + "/models", {"autoload": "false"})
    except Exception as e:  # noqa: BLE001
        raise LlamaError(f"{server} unreachable: {_err(e)}", 502)
    if infer_mode(m) != "router":
        raise LlamaError(f"{server} is a single-model server; it cannot load or unload", 409)
    ids = {x.get("id") for x in (m.get("data") or []) if isinstance(x, dict)}
    if model not in ids:
        raise LlamaError(f"{server} has no model {model!r}", 404)
    status, body = _post(f"{url}/models/{action}", {"model": model})
    ok = status == 200 and isinstance(body, dict) and body.get("success") is True
    msg = None
    if not ok:
        msg = (body or {}).get("error", {}).get("message") if isinstance(body, dict) else None
        msg = msg or f"upstream HTTP {status}"
    return {"ok": ok, "server": server, "action": action, "model": model, "upstream_status": status, "error": msg}


# ------------------------------------------------------------------ chat proxy
CHAT_FIELDS = {"model", "messages", "temperature", "top_p", "max_tokens", "stream", "presence_penalty", "frequency_penalty", "stop"}
CHAT_ROLES = {"system", "user", "assistant"}
CHAT_MAX_MESSAGES = 400


def chat_payload(body: dict) -> dict:
    """Whitelist the client's chat request. Anything not listed is dropped, not forwarded."""
    if not isinstance(body, dict):
        raise LlamaError("body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not (1 <= len(model) <= 128):
        raise LlamaError("model must be a non-empty string")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs or len(msgs) > CHAT_MAX_MESSAGES:
        raise LlamaError(f"messages must be a non-empty list of at most {CHAT_MAX_MESSAGES}")
    clean = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in CHAT_ROLES or not isinstance(m.get("content"), str):
            raise LlamaError("each message needs role (system|user|assistant) and string content")
        clean.append({"role": m["role"], "content": m["content"]})
    out = {"model": model, "messages": clean, "stream": bool(body.get("stream", True))}
    for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if k in body and isinstance(body[k], (int, float)):
            out[k] = float(body[k])
    if "max_tokens" in body and isinstance(body["max_tokens"], int) and 0 < body["max_tokens"] <= 65536:
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("stop"), list) and all(isinstance(x, str) for x in body["stop"]) and len(body["stop"]) <= 8:
        out["stop"] = body["stop"]
    return out


def chat_open(server: str, payload: dict, timeout: float = 600.0):
    """POST /v1/chat/completions upstream; returns (status, content_type, response-like object to stream from).

    Only a model the server currently has LOADED is accepted, so a chat can
    never trigger a load or an eviction. The caller streams ``resp`` and closes it.
    """
    if server not in SERVERS:
        raise LlamaError(f"no llama server named {server!r}", 404)
    url = SERVERS[server]
    try:
        m = _get(url + "/models", {"autoload": "false"})
    except Exception as e:  # noqa: BLE001
        raise LlamaError(f"{server} unreachable: {_err(e)}", 502)
    data = [x for x in (m.get("data") or []) if isinstance(x, dict)] if isinstance(m, dict) else []
    if infer_mode(m) == "router":
        loaded = {x.get("id") for x in data if (x.get("status") or {}).get("value") == "loaded"}
        if payload["model"] not in loaded:
            raise LlamaError(f"{payload['model']!r} is not loaded on {server}; load it from the dashboard first (chat never loads models)", 409)
    else:
        ids = {x.get("id") for x in data} | {(x.get("aliases") or [None])[0] for x in data}
        if data and payload["model"] not in ids:
            raise LlamaError(f"{server} serves {sorted(i for i in ids if i)}, not {payload['model']!r}", 409)
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream, application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e
    except Exception as e:  # noqa: BLE001
        raise LlamaError(f"{server}: {_err(e)}", 502)
    return resp.status, resp.headers.get("Content-Type", "text/event-stream"), resp
