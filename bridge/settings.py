"""Bridge-wide settings that a user changes from the launcher's Settings dialog.

Stored as JSON next to the apps directory (``<state>/settings.json``). Every key
has a whitelist of values; anything else is refused, so the file can never
hold something the server would not have written itself. Environment variables
give the *defaults* (for people who configure via the unit drop-in); the file,
once written, wins.

    home_position   top | left | bottom | right     where the home tab peeks in
"""
from __future__ import annotations

import json
import os
from typing import Optional

ALLOWED: dict[str, tuple[str, ...]] = {
    "home_position": ("top", "left", "bottom", "right"),
}


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, path: str, defaults: Optional[dict] = None):
        self.path = path
        self.defaults = {"home_position": "top"}
        for k, v in (defaults or {}).items():
            if k in ALLOWED and v in ALLOWED[k]:
                self.defaults[k] = v
        self._mtime: Optional[float] = None
        self._data: dict = {}

    def _load(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._mtime, self._data = None, {}
            return
        if st.st_mtime == self._mtime:
            return
        self._mtime = st.st_mtime
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self._data = {k: v for k, v in raw.items() if k in ALLOWED and v in ALLOWED[k]} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def all(self) -> dict:
        self._load()
        return {**self.defaults, **self._data}

    def get(self, key: str) -> str:
        return self.all()[key]

    def update(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("body must be a JSON object of settings")
        for k, v in changes.items():
            if k not in ALLOWED:
                raise SettingsError(f"unknown setting {k!r}; known: {sorted(ALLOWED)}")
            if v not in ALLOWED[k]:
                raise SettingsError(f"{k} must be one of {list(ALLOWED[k])}")
        self._load()
        new = {**self._data, **changes}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, indent=2)
        os.replace(tmp, self.path)
        self._mtime = None
        return self.all()
