"""Read-only filesystem access for the Web-File app. The one place the bridge
accepts a path from a client, so it is fenced three ways:

* **Roots.** ``~/.config/sysbridge/fs.json`` → ``{"roots": ["~"], "show_hidden": false}``.
  No file = those defaults (the user's home). Every request path is resolved
  with ``realpath`` and must lie inside a root; a symlink pointing outside a
  root is refused because realpath follows it first.
* **The sensitive token** (``X-Bridge-Sensitive-Token``) on every call. Apps
  must never store it; Web-File keeps it in page memory only.
* **Read-only.** ls, stat, text preview (≤ 1 MiB, binary detected and refused
  with a hint to download), download. No write, rename, delete, chmod.

Hidden entries (dot-files) are omitted unless ``show_hidden`` or ``?hidden=1``.
"""
from __future__ import annotations

import json
import os
import stat as statmod
import time
from typing import Optional

from .util import xdg_config_home

PREVIEW_MAX = 1 << 20


class FsError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Fs:
    def __init__(self, config_path: Optional[str] = None, roots: Optional[list[str]] = None, show_hidden: Optional[bool] = None):
        self.config_path = config_path or os.path.join(xdg_config_home(), "sysbridge", "fs.json")
        self._mtime: Optional[float] = None
        self._roots: list[str] = []
        self._show_hidden = False
        self._override = (roots, show_hidden)
        self._load()

    def _load(self) -> None:
        roots, hidden = ["~"], False
        if self._override[0] is not None:
            roots = self._override[0]
            hidden = bool(self._override[1])
        else:
            try:
                st = os.stat(self.config_path)
                if st.st_mtime == self._mtime:
                    return
                self._mtime = st.st_mtime
                with open(self.config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                if isinstance(cfg.get("roots"), list) and cfg["roots"]:
                    roots = [str(r) for r in cfg["roots"]]
                hidden = bool(cfg.get("show_hidden", False))
            except (OSError, ValueError):
                self._mtime = None
        seen, out = set(), []
        for r in roots:
            real = os.path.realpath(os.path.expanduser(r))
            if os.path.isdir(real) and real not in seen:
                seen.add(real)
                out.append(real)
        self._roots, self._show_hidden = out, hidden

    def roots(self) -> list[dict]:
        self._load()
        return [{"path": r, "name": os.path.basename(r) or r} for r in self._roots]

    def show_hidden(self) -> bool:
        self._load()
        return self._show_hidden

    def resolve(self, path: str) -> str:
        """Real path inside a root, or FsError. Relative paths are refused."""
        self._load()
        if not isinstance(path, str) or not path or "\x00" in path:
            raise FsError("path required")
        if not path.startswith("/"):
            raise FsError("path must be absolute")
        real = os.path.realpath(path)
        for r in self._roots:
            if real == r or real.startswith(r.rstrip("/") + "/"):
                if not os.path.exists(real):
                    raise FsError("no such file or directory", 404)
                return real
        raise FsError("outside the allowed roots", 403)

    @staticmethod
    def _entry(dirpath: str, name: str) -> Optional[dict]:
        full = os.path.join(dirpath, name)
        try:
            lst = os.lstat(full)
        except OSError:
            return None
        is_link = statmod.S_ISLNK(lst.st_mode)
        try:
            st = os.stat(full) if is_link else lst
        except OSError:
            st = lst
        kind = "dir" if statmod.S_ISDIR(st.st_mode) else "file" if statmod.S_ISREG(st.st_mode) else "other"
        e = {"name": name, "type": kind, "link": is_link, "size": st.st_size if kind == "file" else None,
             "mtime": round(st.st_mtime, 3), "mode": statmod.filemode(lst.st_mode), "hidden": name.startswith(".")}
        if is_link:
            try:
                e["target"] = os.readlink(full)
            except OSError:
                pass
        return e

    def ls(self, path: str, hidden: Optional[bool] = None) -> dict:
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise FsError("not a directory")
        show = self._show_hidden if hidden is None else hidden
        try:
            names = os.listdir(real)
        except PermissionError:
            raise FsError("permission denied", 403)
        entries = [e for e in (self._entry(real, n) for n in names) if e and (show or not e["hidden"])]
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        parent = os.path.dirname(real) if real not in self._roots else None
        return {"path": real, "parent": parent if parent and any(parent == r or parent.startswith(r + "/") for r in self._roots) else None,
                "entries": entries, "hidden_shown": show, "count": len(entries)}

    def stat(self, path: str) -> dict:
        real = self.resolve(path)
        e = self._entry(os.path.dirname(real), os.path.basename(real)) or {}
        return {"path": real, **e}

    def read(self, path: str) -> dict:
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise FsError("not a regular file")
        size = os.path.getsize(real)
        if size > PREVIEW_MAX:
            raise FsError(f"file is {size} bytes; preview is limited to {PREVIEW_MAX} — download it instead", 413)
        with open(real, "rb") as f:
            data = f.read(PREVIEW_MAX)
        if b"\x00" in data[:8192]:
            return {"path": real, "size": size, "binary": True, "text": None}
        return {"path": real, "size": size, "binary": False, "text": data.decode("utf-8", "replace")}

    def open_download(self, path: str):
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise FsError("not a regular file")
        return real, os.path.getsize(real), os.path.basename(real)
