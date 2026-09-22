"""HTTP server: routing, CORS, SSE, token check. Stdlib ThreadingHTTPServer.

Security model (see HANDOVER.md → "Security model"):

* Origin allowlist on every request: exact ``null`` (a file:// page) and
  ``^http://(127\\.0\\.0\\.1|localhost|\\[::1\\])(:\\d+)?$``; ``--origins`` adds
  exact extras. A match is reflected in ``Access-Control-Allow-Origin`` with
  ``Vary: Origin``. No match → 403 **without** CORS headers, so the browser
  hides the body from the page. No Origin header at all (curl) → allowed.
* POST /v1/action/{name} additionally needs ``X-Bridge-Token`` equal to the
  token file in ``$XDG_RUNTIME_DIR/sysbridge/token`` (0600, made at start).
  The token is never served over HTTP; a file:// app gets it pasted once.
* Names are validated against the registry; nothing else from the client is
  ever interpreted (no paths, PIDs or fragments).
* Responses: ``application/json; charset=utf-8``, ``Cache-Control: no-store``,
  no ``Server`` header, body capped at 1 MiB. Handler concurrency capped at 8.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .actions import Actions
from .apps import Apps, AppsError, MAX_APP_BYTES, content_type_for, repo_builtins
from .probes import REG
from .registry import error_envelope, valid_name
from .util import xdg_runtime_dir

LOOPBACK_ORIGIN_RE = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
RESPONSE_CAP = 1 << 20
MAX_STREAM_CLIENTS = 4
MAX_CONCURRENCY = 8
STREAM_MIN_MS, STREAM_MAX_MS, STREAM_DEFAULT_MS = 500, 60000, 1000
START_TS = time.time()


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 80
    extra_origins: list[str] = field(default_factory=list)
    actions_file: Optional[str] = None
    token_file: Optional[str] = None
    apps_root: Optional[str] = None            # default: see apps.apps_root()
    builtins: Optional[dict] = None            # default: apps/<slug>/index.html in the repo


LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www", "launcher.html")
MAX_APP_BODY = MAX_APP_BYTES + 64 * 1024


# ------------------------------------------------------------------ token
def token_path() -> str:
    return os.path.join(xdg_runtime_dir(), "sysbridge", "token")


def ensure_token(path: str) -> str:
    """Create the token file (0600, dir 0700) if missing; return its value."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(t + "\n")
    return t


# ------------------------------------------------------------------ handler
class Bridge(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.actions = Actions(cfg.actions_file)
        self.apps = Apps(cfg.apps_root, cfg.builtins if cfg.builtins is not None else repo_builtins())
        self.token = ensure_token(cfg.token_file or token_path())
        self.sem = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self.stream_clients = 0
        self.stream_lock = threading.Lock()
        fam = socket.AF_INET6 if ":" in cfg.bind else socket.AF_INET
        self.address_family = fam
        super().__init__((cfg.bind, cfg.port), Handler)

    def origin_allowed(self, origin: Optional[str]) -> bool:
        if origin is None:
            return True  # curl and friends: no Origin header
        if origin == "null":
            return True
        if LOOPBACK_ORIGIN_RE.match(origin):
            return True
        return origin in self.cfg.extra_origins


class Handler(BaseHTTPRequestHandler):
    server: Bridge
    protocol_version = "HTTP/1.1"
    server_version = "sysbridge"
    sys_version = ""

    # quieter than the default: one line per request to stderr only for errors/actions
    def log_message(self, fmt, *args):  # noqa: D401
        pass

    def version_string(self):
        return ""  # do not advertise; send_response() adds Server: only if this is non-empty… it isn't, see below

    # -- plumbing ----------------------------------------------------------
    def _origin(self) -> Optional[str]:
        return self.headers.get("Origin")

    def _cors(self, origin: Optional[str]) -> None:
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, status: int, body: object, origin: Optional[str], cors: bool = True, extra: Optional[dict] = None) -> None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) > RESPONSE_CAP:
            data = json.dumps({"ok": False, "error": {"type": "TooLarge", "message": f"response exceeded {RESPONSE_CAP} bytes"}}).encode()
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        if cors:
            self._cors(origin)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_response(self, code, message=None):
        # BaseHTTPRequestHandler.send_response adds Server: and Date:. We want neither Server nor a
        # version string; send_response_only + our own Date keeps it minimal.
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def _forbidden(self) -> None:
        # 403 with NO CORS headers: the page cannot read this response at all.
        self._send(403, {"ok": False, "error": {"type": "OriginForbidden", "message": "origin not allowed"}}, None, cors=False)

    MAX_BODY = 4096

    def _drain_body(self) -> Optional[bytes]:
        """Read the request body up front, whatever the outcome of the request.

        On a keep-alive connection an unread body would be parsed as the next
        request line ("Bad request syntax"). Returns None (and closes the
        connection) when the declared length is over MAX_BODY.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        limit = MAX_APP_BODY if urlsplit(self.path).path == "/v1/apps" else self.MAX_BODY
        if n > limit:
            self.close_connection = True
            return None
        return self.rfile.read(n)

    def _send_file(self, path: str, origin: Optional[str]) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_APP_BODY + 1)
        except OSError:
            return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "file not found"}}, origin)
        if len(data) > MAX_APP_BODY:
            return self._send(500, {"ok": False, "error": {"type": "TooLarge", "message": "file too large to serve"}}, origin)
        self.send_response(200)
        self.send_header("Content-Type", content_type_for(path))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors(origin)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, status: int, location: str) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _token_ok(self) -> bool:
        tok = self.headers.get("X-Bridge-Token", "")
        return bool(tok) and secrets.compare_digest(tok, self.server.token)

    @staticmethod
    def _parse_json(raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            b = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"body is not JSON: {e}")
        return b if isinstance(b, dict) else {}

    # -- methods -------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        origin = self._origin()
        if not self.server.origin_allowed(origin):
            return self._forbidden()
        self.send_response(204)
        self._cors(origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Token, X-Filename")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        origin = self._origin()
        self._drain_body()
        if not self.server.origin_allowed(origin):
            return self._forbidden()
        if not self.server.sem.acquire(timeout=5):
            return self._send(503, {"ok": False, "error": {"type": "Busy", "message": "too many concurrent requests"}}, origin)
        try:
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            parts = [p for p in u.path.split("/") if p]
            # -- launcher and installed apps (same origin as the API on purpose) --
            if u.path == "/" or u.path == "/index.html":
                return self._send_file(LAUNCHER_PATH, origin)
            if parts[:1] == ["apps"] and len(parts) >= 2:
                slug = parts[1]
                m = self.server.apps.get(slug)
                if m is None:
                    return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "no such app"}}, origin)
                if m["kind"] == "link":
                    return self._redirect(302, m["url"])
                if len(parts) == 2 and not u.path.endswith("/"):
                    return self._redirect(301, f"/apps/{slug}/")  # relative URLs inside the app need the slash
                rel = "/".join(parts[2:]) + ("/" if u.path.endswith("/") and len(parts) > 2 else "")
                path = self.server.apps.resolve(slug, rel)
                if path is None:
                    return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "file not found"}}, origin)
                return self._send_file(path, origin)
            if u.path == "/favicon.ico":
                return self._send(404, {"ok": False}, origin)
            if parts[:1] != ["v1"] or len(parts) < 2:
                return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "see /v1/health"}}, origin)
            what = parts[1]
            if what == "apps" and len(parts) == 2:
                return self._send(200, self.server.apps.list(), origin)
            if what == "health" and len(parts) == 2:
                return self._send(200, {"ok": True, "version": __version__, "uptime_s": round(time.time() - START_TS, 1),
                                        "probes": REG.names(), "actions_enabled": self.server.actions.enabled(),
                                        "ts": round(time.time(), 3)}, origin)
            if what == "probes" and len(parts) == 2:
                return self._send(200, REG.describe(), origin)
            if what == "probe" and len(parts) == 3:
                name = parts[2]
                if not valid_name(name) or not REG.has(name):
                    return self._send(404, error_envelope(name[:32], "UnknownProbe", "no such probe"), origin)
                return self._send(200, REG.get(name), origin)
            if what == "all" and len(parts) == 2:
                return self._send(200, self._all(q), origin)
            if what == "stream" and len(parts) == 2:
                return self._stream(q, origin)
            if what == "actions" and len(parts) == 2:
                if not self.server.actions.enabled():
                    return self._send(404, {"ok": False, "error": {"type": "ActionsDisabled", "message": "no actions file; see config/actions.json.example"}}, origin)
                err = self.server.actions.load_error()
                if err:
                    return self._send(500, {"ok": False, "error": {"type": "ActionsInvalid", "message": err}}, origin)
                return self._send(200, self.server.actions.catalogue(), origin)
            return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "unknown endpoint"}}, origin)
        finally:
            self.server.sem.release()

    def do_POST(self) -> None:
        origin = self._origin()
        raw = self._drain_body()
        if not self.server.origin_allowed(origin):
            return self._forbidden()
        if raw is None:
            return self._send(413, {"ok": False, "error": {"type": "BadRequest", "message": f"body over {self.MAX_BODY} bytes"}}, origin)
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if parts == ["v1", "apps"]:
            return self._post_app(raw, origin)
        if parts[:2] != ["v1", "action"] or len(parts) != 3:
            return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "POST only to /v1/action/{name} or /v1/apps"}}, origin)
        name = parts[2][:64]
        acts = self.server.actions
        if not acts.enabled():
            return self._send(404, {"ok": False, "error": {"type": "ActionsDisabled", "message": "no actions file"}}, origin)
        # token before anything else that could leak whether an action exists
        if not self._token_ok():
            return self._send(401, {"ok": False, "error": {"type": "Unauthorized", "message": "missing or wrong X-Bridge-Token"}}, origin)
        spec = acts.get(name)
        if spec is None:
            return self._send(404, {"ok": False, "error": {"type": "UnknownAction", "message": "no such action"}}, origin)
        try:
            body = self._parse_json(raw)
        except ValueError as e:
            return self._send(400, {"ok": False, "error": {"type": "BadRequest", "message": str(e)}}, origin)
        if spec["confirm"] and body.get("confirm") is not True:
            return self._send(400, {"ok": False, "error": {"type": "ConfirmRequired", "message": 'this action needs {"confirm": true}'}}, origin)
        if not self.server.sem.acquire(timeout=5):
            return self._send(503, {"ok": False, "error": {"type": "Busy", "message": "too many concurrent requests"}}, origin)
        try:
            result = acts.execute(name, origin)
        finally:
            self.server.sem.release()
        return self._send(200, result, origin)

    def _post_app(self, raw: Optional[bytes], origin: Optional[str]) -> None:
        """Install an app. text/html body = the page itself; application/json = a link app."""
        if not self._token_ok():
            return self._send(401, {"ok": False, "error": {"type": "Unauthorized", "message": "missing or wrong X-Bridge-Token"}}, origin)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if ctype == "application/json":
                body = self._parse_json(raw or b"")
                if body.get("kind", "link") != "link":
                    raise AppsError('JSON uploads must be {"kind": "link", "url": …}')
                m = self.server.apps.install_link(str(body.get("url", "")), body.get("title"), body.get("icon"), body.get("description"))
            else:
                if ctype not in ("text/html", "application/xhtml+xml", "text/plain", "application/octet-stream", ""):
                    raise AppsError(f"unsupported Content-Type {ctype!r}; send text/html or application/json", 415)
                from urllib.parse import unquote
                fname = unquote(self.headers.get("X-Filename", ""))[:200] or None
                m = self.server.apps.install_html((raw or b"").decode("utf-8", "replace"), fname)
        except AppsError as e:
            return self._send(e.status, {"ok": False, "error": {"type": "AppsError", "message": str(e)}}, origin)
        except ValueError as e:
            return self._send(400, {"ok": False, "error": {"type": "BadRequest", "message": str(e)}}, origin)
        print(f"sysbridge: app installed {m['slug']} kind={m['kind']} size={m['size']} replaced={m.get('replaced')} origin={origin or '-'}",
              file=sys.stderr, flush=True)
        return self._send(201, {"ok": True, "app": m}, origin)

    def do_DELETE(self) -> None:
        origin = self._origin()
        raw = self._drain_body()
        if not self.server.origin_allowed(origin):
            return self._forbidden()
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if parts[:2] != ["v1", "apps"] or len(parts) != 3:
            return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": "DELETE only /v1/apps/{slug}"}}, origin)
        if not self._token_ok():
            return self._send(401, {"ok": False, "error": {"type": "Unauthorized", "message": "missing or wrong X-Bridge-Token"}}, origin)
        try:
            body = self._parse_json(raw or b"")
        except ValueError as e:
            return self._send(400, {"ok": False, "error": {"type": "BadRequest", "message": str(e)}}, origin)
        if body.get("confirm") is not True:
            return self._send(400, {"ok": False, "error": {"type": "ConfirmRequired", "message": 'uninstall needs {"confirm": true}'}}, origin)
        try:
            moved = self.server.apps.uninstall(parts[2][:64])
        except AppsError as e:
            return self._send(e.status, {"ok": False, "error": {"type": "AppsError", "message": str(e)}}, origin)
        print(f"sysbridge: app uninstalled {parts[2][:64]} -> {os.path.basename(moved)} origin={origin or '-'}", file=sys.stderr, flush=True)
        return self._send(200, {"ok": True, "trashed": os.path.basename(moved)}, origin)

    # -- helpers -------------------------------------------------------------
    def _names(self, q: dict) -> Optional[list[str]]:
        raw = ",".join(q.get("names", []))
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return names or None

    def _all(self, q: dict) -> dict:
        names = self._names(q)
        results = {}
        for n in (names or REG.names()):
            if not valid_name(n):
                results[n[:32]] = error_envelope(n[:32], "BadName", "names must match ^[a-z0-9_]{1,32}$")
            else:
                results[n] = REG.get(n)
        return {"ok": True, "ts": round(time.time(), 3), "results": results}

    def _stream(self, q: dict, origin: Optional[str]) -> None:
        try:
            interval = int(q.get("interval_ms", [STREAM_DEFAULT_MS])[0])
        except ValueError:
            interval = STREAM_DEFAULT_MS
        interval = max(STREAM_MIN_MS, min(STREAM_MAX_MS, interval))
        with self.server.stream_lock:
            if self.server.stream_clients >= MAX_STREAM_CLIENTS:
                return self._send(429, {"ok": False, "error": {"type": "TooManyStreams", "message": f"at most {MAX_STREAM_CLIENTS} stream clients"}}, origin)
            self.server.stream_clients += 1
        # the stream holds a handler slot for as long as it lives; release the semaphore so it does
        # not count against the request budget.
        self.server.sem.release()
        released = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self._cors(origin)
            self.end_headers()
            while True:
                body = json.dumps(self._all(q), separators=(",", ":"), ensure_ascii=False)
                self.wfile.write(f"event: probes\ndata: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(interval / 1000.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.server.stream_lock:
                self.server.stream_clients -= 1
            if released:
                self.server.sem.acquire(timeout=0)  # re-take so the outer finally's release balances
            self.close_connection = True


def serve(cfg: Config) -> None:
    try:
        srv = Bridge(cfg)
    except PermissionError:
        print(f"sysbridge: cannot bind {cfg.bind}:{cfg.port} — ports below 1024 need a one-time kernel setting.\n"
              f"  As root:  printf 'net.ipv4.ip_unprivileged_port_start = 80\\n' > /etc/sysctl.d/80-sysbridge.conf && sysctl -p /etc/sysctl.d/80-sysbridge.conf\n"
              f"  (lowers the threshold for all users; reverse by deleting the file and setting 1024)\n"
              f"  Or run with --port 8182 for now.", file=sys.stderr, flush=True)
        raise SystemExit(2)
    except OSError as e:
        print(f"sysbridge: cannot bind {cfg.bind}:{cfg.port}: {e.strerror or e} (something else listening? try --port 8182)", file=sys.stderr, flush=True)
        raise SystemExit(2)
    port = "" if cfg.port == 80 else f":{cfg.port}"
    print(f"sysbridge {__version__} launcher at http://localhost{port}/  probes={len(REG.names())}  apps={len(srv.apps.list())}  "
          f"actions={'enabled' if srv.actions.enabled() else 'disabled (no actions file)'}  token={token_path()}",
          file=sys.stderr, flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
