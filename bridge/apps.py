"""Installed web apps: the launcher's catalogue, upload, uninstall, static resolution.

An app is one directory under the apps root::

    <apps root>/<slug>/index.html      the uploaded page (html apps)
    <apps root>/<slug>/manifest.json   generated: title, icon, kind, …
    <apps root>/.trash/<slug>-<ts>/    replaced or uninstalled apps, never deleted

Conventions instead of configuration: the title comes from ``<title>``, the
subtitle from ``<meta name="description">``, the icon from
``<meta name="app-icon" content="🦙">`` (fallback: initials); the slug is the
slugified title and is the URL. Link apps have a manifest only and 302 to
their URL. Built-in apps live in the repo and cannot be uninstalled.

The apps root is ``$STATE_DIRECTORY/apps`` under systemd (``StateDirectory=``),
else ``$XDG_STATE_HOME/sysbridge/apps``, else ``~/.local/state/sysbridge/apps``.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import time
import unicodedata
from typing import Optional

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_APP_BYTES = 4 * 1024 * 1024
_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_RE = re.compile(r"<meta\s+[^>]*>", re.I | re.S)
_ATTR_RE = re.compile(r"""([a-zA-Z_:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8", ".wasm": "application/wasm",
}


class AppsError(ValueError):
    """Client-caused error; ``status`` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ pure helpers
def apps_root() -> str:
    if os.environ.get("STATE_DIRECTORY"):
        return os.path.join(os.environ["STATE_DIRECTORY"].split(":")[0], "apps")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "sysbridge", "apps")


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:48].strip("-")
    if not s or not SLUG_RE.match(s):
        raise AppsError(f"cannot make a slug from title {title!r}")
    return s


def initials(title: str) -> str:
    words = [w for w in re.split(r"[\s_-]+", title.strip()) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _attrs(tag: str) -> dict:
    # findall yields '' (not None) for the groups that did not match, hence `or`
    return {k.lower(): (v1 or v2 or v3 or "") for k, v1, v2, v3 in _ATTR_RE.findall(tag)}


def parse_html_meta(text: str, fallback_title: str = "") -> dict:
    """``<title>``, ``<meta name=description>``, ``<meta name=app-icon>`` → dict.

    Regex on purpose: the stdlib HTML parser is fine but this only needs the
    head, and a malformed body must not stop an install.
    """
    head = text[:64 * 1024]
    m = _TITLE_RE.search(head)
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    if not title:
        title = fallback_title.strip()
    desc, icon = "", ""
    for tag in _META_RE.findall(head):
        a = _attrs(tag)
        name = (a.get("name") or a.get("property") or "").lower()
        if name == "description" and not desc:
            desc = html.unescape(a.get("content", "")).strip()
        elif name == "app-icon" and not icon:
            icon = html.unescape(a.get("content", "")).strip()
    return {"title": title, "description": desc[:200], "icon": icon[:8] or initials(title)}


def content_type_for(path: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


# ------------------------------------------------------------------ the store
class Apps:
    def __init__(self, root: Optional[str] = None, builtins: Optional[dict[str, str]] = None):
        self.root = root or apps_root()
        # slug -> directory holding index.html (served read-only from the repo)
        self.builtins: dict[str, str] = dict(builtins or {})

    # -- listing -------------------------------------------------------------
    def _read_manifest(self, d: str) -> Optional[dict]:
        try:
            with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
            return m if isinstance(m, dict) and SLUG_RE.match(str(m.get("slug", ""))) else None
        except (OSError, ValueError):
            return None

    def _builtin_manifest(self, slug: str) -> dict:
        d = self.builtins[slug]
        path = os.path.join(d, "index.html")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                meta = parse_html_meta(f.read(), fallback_title=slug)
            size = os.path.getsize(path)
        except OSError:
            meta, size = {"title": slug, "description": "", "icon": initials(slug)}, 0
        return {"slug": slug, "kind": "html", "builtin": True, "url": None, "installed_at": None,
                "size": size, "sha256": None, "original_filename": None, **meta}

    def list(self) -> list[dict]:
        out = [self._builtin_manifest(s) for s in sorted(self.builtins)]
        installed = []
        if os.path.isdir(self.root):
            for name in os.listdir(self.root):
                if name.startswith(".") or not SLUG_RE.match(name) or name in self.builtins:
                    continue
                m = self._read_manifest(os.path.join(self.root, name))
                if m and m["slug"] == name:
                    installed.append(m)
        out.extend(sorted(installed, key=lambda m: m["title"].lower()))
        return out

    def get(self, slug: str) -> Optional[dict]:
        if not SLUG_RE.match(slug or ""):
            return None
        if slug in self.builtins:
            return self._builtin_manifest(slug)
        return self._read_manifest(os.path.join(self.root, slug))

    # -- static resolution -----------------------------------------------------
    def resolve(self, slug: str, relpath: str) -> Optional[str]:
        """Absolute path of ``relpath`` inside the app's directory, or None.

        Rejects anything that would escape the directory (``..``, absolute
        paths, symlinks pointing outside). ``""`` and ``"/"`` mean index.html.
        """
        if not SLUG_RE.match(slug or ""):
            return None
        base = self.builtins.get(slug) or os.path.join(self.root, slug)
        base = os.path.realpath(base)
        rel = (relpath or "").lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        if "\x00" in rel or any(part in ("", ".", "..") for part in rel.split("/")):
            return None
        full = os.path.realpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            return None
        if not os.path.isfile(full):
            return None
        return full

    # -- install / uninstall --------------------------------------------------
    def _trash(self, slug: str) -> Optional[str]:
        src = os.path.join(self.root, slug)
        if not os.path.isdir(src):
            return None
        tdir = os.path.join(self.root, ".trash")
        os.makedirs(tdir, exist_ok=True)
        dst = os.path.join(tdir, f"{slug}-{time.strftime('%Y%m%dT%H%M%S')}")
        n = 1
        while os.path.exists(dst):
            dst = f"{dst}-{n}"
            n += 1
        shutil.move(src, dst)
        return dst

    def install_html(self, text: str, original_filename: Optional[str] = None) -> dict:
        if not text or not text.strip():
            raise AppsError("empty upload")
        if len(text.encode("utf-8")) > MAX_APP_BYTES:
            raise AppsError(f"app over {MAX_APP_BYTES} bytes", 413)
        if "<" not in text[:4096]:
            raise AppsError("that does not look like HTML")
        fallback = os.path.splitext(os.path.basename(original_filename or ""))[0].replace("_", " ").replace("-", " ")
        meta = parse_html_meta(text, fallback_title=fallback)
        if not meta["title"]:
            raise AppsError("the page needs a <title> (or upload it with a filename to use instead)")
        slug = slugify(meta["title"])
        if slug in self.builtins:
            raise AppsError(f"'{slug}' is a built-in app and cannot be replaced", 409)
        data = text.encode("utf-8")
        manifest = {"slug": slug, "kind": "html", "builtin": False, "url": None,
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "original_filename": (original_filename or "")[:120] or None,
                    **meta}
        replaced = self._trash(slug)
        d = os.path.join(self.root, slug)
        os.makedirs(d, mode=0o755, exist_ok=True)
        tmp = os.path.join(d, ".index.html.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(d, "index.html"))
        self._write_manifest(d, manifest)
        manifest["replaced"] = os.path.basename(replaced) if replaced else None
        return manifest

    def install_link(self, url: str, title: Optional[str] = None, icon: Optional[str] = None,
                     description: Optional[str] = None) -> dict:
        url = (url or "").strip()
        if not _URL_RE.match(url) or len(url) > 2048:
            raise AppsError("url must start with http:// or https://")
        title = (title or "").strip()
        if not title:
            host = re.sub(r"^https?://", "", url).split("/")[0]
            title = host
        slug = slugify(title)
        if slug in self.builtins:
            raise AppsError(f"'{slug}' is a built-in app", 409)
        manifest = {"slug": slug, "kind": "link", "builtin": False, "url": url, "title": title[:80],
                    "description": (description or "")[:200], "icon": (icon or "").strip()[:8] or initials(title),
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "size": 0, "sha256": None,
                    "original_filename": None}
        replaced = self._trash(slug)
        d = os.path.join(self.root, slug)
        os.makedirs(d, mode=0o755, exist_ok=True)
        self._write_manifest(d, manifest)
        manifest["replaced"] = os.path.basename(replaced) if replaced else None
        return manifest

    def uninstall(self, slug: str) -> str:
        if not SLUG_RE.match(slug or ""):
            raise AppsError("bad slug", 404)
        if slug in self.builtins:
            raise AppsError(f"'{slug}' is built in and cannot be uninstalled", 403)
        moved = self._trash(slug)
        if moved is None:
            raise AppsError("no such app", 404)
        return moved

    @staticmethod
    def _write_manifest(d: str, manifest: dict) -> None:
        tmp = os.path.join(d, ".manifest.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, "manifest.json"))


def repo_builtins() -> dict[str, str]:
    """Built-in apps shipped in the repo: ``apps/<slug>/index.html``."""
    here = os.path.dirname(os.path.abspath(__file__))
    apps_dir = os.path.join(os.path.dirname(here), "apps")
    out = {}
    if os.path.isdir(apps_dir):
        for name in sorted(os.listdir(apps_dir)):
            d = os.path.join(apps_dir, name)
            if SLUG_RE.match(name) and os.path.isfile(os.path.join(d, "index.html")):
                out[name] = d
    return out
