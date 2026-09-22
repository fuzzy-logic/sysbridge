"""The `top` probe: htop's view of the machine from /proc, no subprocess.

Per-process CPU is instantaneous — the delta of utime+stime between two
samples over the wall time between them, 100 % = one core — because that is
what htop shows and what `ps -o pcpu` does not (it reports the lifetime
average). The first sample after start therefore reports 0 % for everyone.

Per call: /proc/stat (per-core), /proc/meminfo, /proc/loadavg, /proc/uptime,
and for every pid /proc/<pid>/stat, /status (VmRSS, Threads, Uid) and /cmdline.
~5 000 processes cost roughly 60–120 ms of Python on the reference machine,
hence ttl 1500 ms and async refresh so no request ever waits on it.
"""
from __future__ import annotations

import os
import pwd
import time
from typing import Optional

from . import parsers
from .registry import Registry
from .util import read_text

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
TOP_N = 200

_prev: dict = {"ts": 0.0, "procs": {}, "cpus": None}
_users: dict[int, str] = {}


def _user(uid: int) -> str:
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _users[uid] = str(uid)
    return _users[uid]


def parse_stat_line(line: str) -> Optional[dict]:
    """/proc/<pid>/stat → fields we use. comm may contain spaces and parens."""
    try:
        lp, rp = line.index("("), line.rindex(")")
        comm = line[lp + 1:rp]
        f = line[rp + 2:].split()
        return {"comm": comm, "state": f[0], "ppid": int(f[1]), "utime": int(f[11]), "stime": int(f[12]),
                "threads": int(f[17]), "starttime": int(f[19]), "rss_pages": int(f[21])}
    except (ValueError, IndexError):
        return None


def parse_stat_cpus(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("cpu") and not line.startswith("cpu "):
            f = [int(x) for x in line.split()[1:]]
            idle = f[3] + (f[4] if len(f) > 4 else 0)
            out.append({"idle": idle, "total": sum(f)})
    return out


def sample() -> dict:
    now = time.time()
    dt = now - _prev["ts"] if _prev["ts"] else 0.0
    cpus_now = parse_stat_cpus(read_text("/proc/stat", ""))
    per_core = []
    if _prev["cpus"] and len(_prev["cpus"]) == len(cpus_now):
        for a, b in zip(_prev["cpus"], cpus_now):
            dtot, didle = b["total"] - a["total"], b["idle"] - a["idle"]
            per_core.append(round(100.0 * (1 - didle / dtot), 1) if dtot > 0 else 0.0)
    else:
        per_core = [0.0] * len(cpus_now)

    procs, cur = [], {}
    total_procs = running = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        st = read_text(f"/proc/{name}/stat")
        if not st:
            continue
        p = parse_stat_line(st)
        if not p:
            continue
        total_procs += 1
        if p["state"] == "R":
            running += 1
        ticks = p["utime"] + p["stime"]
        key = (pid, p["starttime"])
        cur[key] = ticks
        prev = _prev["procs"].get(key)
        cpu = round(100.0 * (ticks - prev) / CLK_TCK / dt, 1) if (prev is not None and dt > 0) else 0.0
        try:
            uid = os.stat(f"/proc/{name}").st_uid
        except OSError:
            uid = -1
        cmd = read_text(f"/proc/{name}/cmdline", "") or ""
        cmd = cmd.replace("\x00", " ").strip()[:200]
        procs.append({"pid": pid, "ppid": p["ppid"], "user": _user(uid) if uid >= 0 else "?", "state": p["state"],
                      "threads": p["threads"], "cpu": max(cpu, 0.0), "rss": p["rss_pages"] * PAGE,
                      "comm": p["comm"], "cmd": cmd or f"[{p['comm']}]"})
    _prev.update({"ts": now, "procs": cur, "cpus": cpus_now})

    mi = parsers.parse_meminfo(read_text("/proc/meminfo", ""))
    total = mi.get("MemTotal", 0) or 1
    for p in procs:
        p["mem"] = round(100.0 * p["rss"] / total, 1)
    procs.sort(key=lambda p: (-p["cpu"], -p["rss"]))
    la = parsers.parse_loadavg(read_text("/proc/loadavg", "0 0 0 0/0 0"))
    up = read_text("/proc/uptime", "0 0").split()
    return {
        "ts": round(now, 3),
        "interval_s": round(dt, 3),
        "uptime_s": float(up[0]) if up else None,
        "load": la["load"],
        "tasks": {"total": total_procs, "running": running, "threads": sum(p["threads"] for p in procs)},
        "cpu": {"cores": per_core, "busy_percent": round(sum(per_core) / len(per_core), 1) if per_core else None, "nproc": len(cpus_now)},
        "mem": {"total": mi.get("MemTotal"), "used": (mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)), "available": mi.get("MemAvailable"),
                "buffers": mi.get("Buffers"), "cached": mi.get("Cached"), "shmem": mi.get("Shmem"),
                "swap_total": mi.get("SwapTotal"), "swap_used": (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0))},
        "processes": procs[:TOP_N],
        "truncated": len(procs) > TOP_N,
    }


def register(reg: Registry) -> None:
    @reg.probe("top", ttl_ms=1500, description="htop's view: per-core busy, per-process instantaneous CPU/RSS/user/state/threads/cmd, tasks, load, uptime", async_refresh=True)
    def top() -> dict:
        return sample()
