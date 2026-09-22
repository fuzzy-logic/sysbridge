"""Probe and action registries with per-entry TTL cache, lock and error isolation.

A probe is a plain function returning JSON-serialisable data, registered with
``@probe(name, ttl_ms, description)``. Calling ``get(name)`` returns an
*envelope*::

    {ok, name, ts, ttl_ms, stale, data|null, error:{type,message}|null}

``stale=true`` means the cached value is past its TTL and the refresh failed;
clients keep showing the last good value. Every exception raised by a probe is
caught and becomes an error envelope, so ``/v1/all`` is never broken by one
probe. Each probe has its own lock and cache, so a slow subprocess-backed probe
(rocm-smi, ~1 s) never blocks the sysfs ones.

``async_refresh=True`` probes are refreshed in a background thread once stale:
the request that notices staleness gets the cached value immediately and kicks
off the refresh, so after warm-up no HTTP request ever waits on a subprocess.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional

NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class ProbeEntry:
    def __init__(self, name: str, fn: Callable[[], object], ttl_ms: int, description: str, async_refresh: bool):
        self.name = name
        self.fn = fn
        self.ttl_ms = ttl_ms
        self.description = description
        self.async_refresh = async_refresh
        self.lock = threading.Lock()
        self.data: object = None
        self.ts: float = 0.0            # time of last successful refresh
        self.last_ok_ts: float = 0.0
        self.last_error: Optional[dict] = None
        self.refreshing = False

    # -- cache logic ---------------------------------------------------------
    def fresh(self, now: float) -> bool:
        return self.ts > 0 and (now - self.ts) * 1000.0 < self.ttl_ms

    def _refresh(self) -> None:
        """Run the probe once and update the cache. Never raises."""
        try:
            data = self.fn()
            with self.lock:
                self.data = data
                self.ts = self.last_ok_ts = time.time()
                self.last_error = None
        except Exception as e:  # noqa: BLE001 — isolation is the point
            with self.lock:
                self.last_error = {"type": type(e).__name__, "message": str(e) or repr(e)}
        finally:
            with self.lock:
                self.refreshing = False

    def _start_async(self) -> None:
        with self.lock:
            if self.refreshing:
                return
            self.refreshing = True
        threading.Thread(target=self._refresh, name=f"probe-{self.name}", daemon=True).start()

    def get(self, force: bool = False) -> dict:
        now = time.time()
        with self.lock:
            fresh = self.fresh(now) and not force
            have = self.ts > 0
        if not fresh:
            if self.async_refresh and have and not force:
                self._start_async()
            else:
                with self.lock:
                    self.refreshing = True
                self._refresh()
        with self.lock:
            now = time.time()
            ok = self.ts > 0 and self.last_error is None
            stale = self.ts > 0 and not self.fresh(now) and self.last_error is not None
            return {
                "ok": ok or (self.ts > 0 and self.last_error is None),
                "name": self.name,
                "ts": round(self.ts if self.ts else now, 3),
                "ttl_ms": self.ttl_ms,
                "stale": stale,
                "data": self.data if self.ts > 0 else None,
                "error": self.last_error,
            }

    def describe(self) -> dict:
        with self.lock:
            return {
                "name": self.name,
                "description": self.description,
                "ttl_ms": self.ttl_ms,
                "async_refresh": self.async_refresh,
                "last_ok_ts": round(self.last_ok_ts, 3) if self.last_ok_ts else None,
                "last_error": self.last_error,
            }


class Registry:
    def __init__(self) -> None:
        self._probes: dict[str, ProbeEntry] = {}

    def probe(self, name: str, ttl_ms: int = 1000, description: str = "", async_refresh: bool = False):
        if not NAME_RE.match(name):
            raise ValueError(f"bad probe name {name!r}")
        if name in self._probes:
            raise ValueError(f"duplicate probe {name!r}")

        def deco(fn: Callable[[], object]):
            self._probes[name] = ProbeEntry(name, fn, ttl_ms, description or (fn.__doc__ or "").strip().split("\n")[0], async_refresh)
            return fn
        return deco

    def names(self) -> list[str]:
        return sorted(self._probes)

    def has(self, name: str) -> bool:
        return name in self._probes

    def get(self, name: str, force: bool = False) -> dict:
        e = self._probes.get(name)
        if e is None:
            return error_envelope(name, "UnknownProbe", f"no probe named {name!r}")
        return e.get(force=force)

    def get_many(self, names: Optional[list[str]] = None, force: bool = False) -> dict:
        names = list(names) if names else self.names()
        return {n: self.get(n, force=force) for n in names}

    def describe(self) -> list[dict]:
        return [self._probes[n].describe() for n in self.names()]


def error_envelope(name: str, etype: str, message: str) -> dict:
    return {"ok": False, "name": name, "ts": round(time.time(), 3), "ttl_ms": 0, "stale": False,
            "data": None, "error": {"type": etype, "message": message}}


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))
