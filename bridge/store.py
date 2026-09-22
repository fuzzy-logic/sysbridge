"""The app store: a catalogue folder in the repo, installable with one click.

    store/<slug>/index.html      one single-file app per folder; a PR adds a folder

Metadata comes from the file itself (the same conventions as an upload):
``<title>``, ``<meta name="description">``, ``<meta name="app-icon">``, plus the
optional ``<meta name="app-version">`` and ``<meta name="app-author">``. The
folder name must equal the slug of the title, so the URL a reader sees in the
PR is the URL the app will get.

Installing copies the file through the same path as an upload
(``Apps.install_html``), so an installed store app is indistinguishable from
an uploaded one and uninstalls the same way. ``installed_sha256`` vs ``sha256``
tells the store UI when an update is available.

``validate()`` is what the catalogue test and the PR check run: an app must
have a title and an icon, be one file under 4 MiB, load no external script or
stylesheet, and call no other host — its origin is the bridge, full stop.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from .apps import SLUG_RE, MAX_APP_BYTES, Apps, parse_html_meta, slugify, _META_RE, _attrs

_EXT_SRC_RE = re.compile(r"""<(?:script|link|iframe|img|video|audio|source)\b[^>]*\b(?:src|href)\s*=\s*["']?\s*(https?:)?//""", re.I)
_HARDCODED_HOST_RE = re.compile(r"""https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?/(?!\s*[)"'`]*\s*(?:\*|//|#))""")
_HOME_LINK_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["']/["']""", re.I)


def store_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "store")


def extra_meta(text: str) -> dict:
    out = {"version": "", "author": ""}
    for tag in _META_RE.findall(text[:64 * 1024]):
        a = _attrs(tag)
        name = (a.get("name") or "").lower()
        if name == "app-version":
            out["version"] = a.get("content", "").strip()[:32]
        elif name == "app-author":
            out["author"] = a.get("content", "").strip()[:80]
    return out


def validate(slug: str, text: str) -> list[str]:
    """Problems with a catalogue entry; empty list = fine. Used by tests and the PR check."""
    problems = []
    if not SLUG_RE.match(slug):
        problems.append(f"folder name {slug!r} is not a valid slug")
    if len(text.encode("utf-8")) > MAX_APP_BYTES:
        problems.append(f"over {MAX_APP_BYTES} bytes")
    meta = parse_html_meta(text)
    if not meta["title"]:
        problems.append("no <title>")
    else:
        try:
            if slugify(meta["title"]) != slug:
                problems.append(f"folder {slug!r} must equal the slug of the title ({slugify(meta['title'])!r})")
        except ValueError as e:
            problems.append(str(e))
    if not re.search(r'<meta\s[^>]*name\s*=\s*["\']?app-icon', text[:64 * 1024], re.I):
        problems.append('no <meta name="app-icon"> (the tile would fall back to initials)')
    if not re.search(r'<meta\s[^>]*name\s*=\s*["\']?description', text[:64 * 1024], re.I):
        problems.append('no <meta name="description"> (the tile would have no subtitle)')
    if _EXT_SRC_RE.search(text):
        problems.append("loads an external script/stylesheet/media (src or href to another host); everything must be inline")
    m = _HARDCODED_HOST_RE.search(text)
    if m:
        problems.append(f"hardcodes a local host URL ({m.group(0)!r}); use location.origin so the app follows its own origin")
    if _HOME_LINK_RE.search(text) and 'name="sysbridge-home"' not in text:
        problems.append('draws its own home link; the bridge injects one — remove it or opt out with <meta name="sysbridge-home" content="none">')
    return problems


class Store:
    def __init__(self, root: Optional[str] = None):
        self.root = root or store_root()

    def _read(self, slug: str) -> Optional[str]:
        if not SLUG_RE.match(slug or ""):
            return None
        p = os.path.join(self.root, slug, "index.html")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(MAX_APP_BYTES + 1)
        except OSError:
            return None

    def entry(self, slug: str, apps: Optional[Apps] = None) -> Optional[dict]:
        text = self._read(slug)
        if text is None:
            return None
        data = text.encode("utf-8")
        meta = parse_html_meta(text, fallback_title=slug)
        e = {"slug": slug, **meta, **extra_meta(text), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
             "problems": validate(slug, text), "installed": False, "installed_sha256": None, "update_available": False, "builtin": False}
        if apps is not None:
            m = apps.get(slug)
            if m is not None:
                e["installed"] = True
                e["builtin"] = bool(m.get("builtin"))
                e["installed_sha256"] = m.get("sha256")
                e["update_available"] = bool(m.get("sha256")) and m.get("sha256") != e["sha256"]
        return e

    def list(self, apps: Optional[Apps] = None) -> list[dict]:
        out = []
        if os.path.isdir(self.root):
            for name in sorted(os.listdir(self.root)):
                if SLUG_RE.match(name) and os.path.isfile(os.path.join(self.root, name, "index.html")):
                    e = self.entry(name, apps)
                    if e:
                        out.append(e)
        return sorted(out, key=lambda e: e["title"].lower())

    def install(self, slug: str, apps: Apps) -> dict:
        text = self._read(slug)
        if text is None:
            raise KeyError(slug)
        return apps.install_html(text, original_filename=f"store/{slug}/index.html")
