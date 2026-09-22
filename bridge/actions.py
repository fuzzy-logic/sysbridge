"""Allowlisted actions: a name in a JSON file maps to a fixed argv.

    ~/.config/sysbridge/actions.json   ($XDG_CONFIG_HOME aware)
    {"actions": {"<name>": {"argv": ["…"], "description": "…", "confirm": true}}}

Rules, in order of how much they matter:

* **Absent file = actions disabled.** ``/v1/actions`` and every POST return 404.
  Nothing is enabled by installing the bridge; the user copies the example
  into place and edits it.
* **Argv is fixed in the file. No client-supplied arguments, ever, in v1.**
  The request body may carry ``{"confirm": true}`` and nothing else is read.
* Names must match ``^[a-z0-9_-]{1,32}$`` and argv must be a non-empty list of
  strings; anything else in the file is rejected at load with a clear error.
* ``confirm: true`` (the default) requires the client to send ``confirm: true``.
* Every run logs one line to stderr (name, exit code, ms, origin) → journald.

The file is re-read when its mtime changes, so editing it needs no restart.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Optional

from .util import run, xdg_config_home

ACTION_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ACTION_TIMEOUT_S = 30.0


class ActionsError(ValueError):
    pass


class Actions:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(xdg_config_home(), "sysbridge", "actions.json")
        self._mtime: Optional[float] = None
        self._actions: dict[str, dict] = {}
        self._load_error: Optional[str] = None

    # -- loading -------------------------------------------------------------
    def enabled(self) -> bool:
        self._maybe_reload()
        return self._mtime is not None

    def _maybe_reload(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            self._mtime, self._actions, self._load_error = None, {}, None
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self._actions = self._validate(raw)
            self._load_error = None
        except (OSError, ValueError) as e:
            self._actions = {}
            self._load_error = f"{self.path}: {e}"
            print(f"sysbridge: actions file rejected: {self._load_error}", file=sys.stderr, flush=True)
        self._mtime = mtime

    @staticmethod
    def _validate(raw: object) -> dict[str, dict]:
        if not isinstance(raw, dict) or not isinstance(raw.get("actions"), dict):
            raise ActionsError('top level must be {"actions": {...}}')
        out = {}
        for name, spec in raw["actions"].items():
            if not ACTION_NAME_RE.match(name):
                raise ActionsError(f"bad action name {name!r}")
            if not isinstance(spec, dict):
                raise ActionsError(f"{name}: spec must be an object")
            argv = spec.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise ActionsError(f"{name}: argv must be a non-empty list of non-empty strings")
            out[name] = {
                "argv": [os.path.expanduser(a) if a.startswith("~/") else a for a in argv],
                "description": str(spec.get("description", "")),
                "confirm": bool(spec.get("confirm", True)),
                "timeout_s": float(spec.get("timeout_s", ACTION_TIMEOUT_S)),
            }
        return out

    # -- API -----------------------------------------------------------------
    def catalogue(self) -> list[dict]:
        self._maybe_reload()
        return [{"name": n, "description": a["description"], "confirm": a["confirm"]} for n, a in sorted(self._actions.items())]

    def load_error(self) -> Optional[str]:
        self._maybe_reload()
        return self._load_error

    def get(self, name: str) -> Optional[dict]:
        self._maybe_reload()
        return self._actions.get(name)

    def execute(self, name: str, origin: Optional[str]) -> dict:
        """Run the allowlisted argv for ``name``. Caller has already checked token/confirm."""
        spec = self.get(name)
        if spec is None:
            raise KeyError(name)
        t0 = time.monotonic()
        try:
            r = run(spec["argv"], timeout=spec["timeout_s"])
            result = {"ok": r["exit_code"] == 0, "name": name, "exit_code": r["exit_code"], "stdout": r["stdout"],
                      "stderr": r["stderr"], "ms": r["ms"], "truncated": r["truncated"]}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "name": name, "exit_code": None, "stdout": "", "stderr": str(e),
                      "ms": int((time.monotonic() - t0) * 1000), "truncated": False}
        print(f"sysbridge: action {name} exit={result['exit_code']} ms={result['ms']} origin={origin or '-'}",
              file=sys.stderr, flush=True)
        return result
