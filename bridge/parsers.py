"""Pure string → dict parsers. No I/O, so every one is unit-tested on fixtures.

Each parser takes the raw text of one command or file and returns plain JSON-
serialisable data. Numbers are bytes unless the key says otherwise.
"""
from __future__ import annotations

import re
from typing import Optional


def parse_meminfo(text: str) -> dict:
    """/proc/meminfo → bytes. Keys are the kernel's, values converted from kB."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)(?:\s+(kB))?", line)
        if not m:
            continue
        v = int(m.group(2))
        if m.group(3) == "kB":
            v *= 1024
        out[m.group(1)] = v
    return out


def parse_df(text: str) -> list[dict]:
    """``df -B1 --output=source,fstype,size,used,avail,target`` → deduped rows.

    btrfs shows one line per mounted subvolume, all with the same source and
    the same numbers. Keep one row per source:
    the one with the shortest mount target, which is the filesystem root when
    it is mounted. Pseudo filesystems that df still prints (efivarfs) are kept
    only when they are real block devices or a named non-pseudo type.
    """
    rows = []
    lines = text.splitlines()
    for line in lines[1:] if lines and lines[0].lower().startswith("filesystem") else lines:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        source, fstype, size, used, avail, target = parts
        try:
            row = {"source": source, "fstype": fstype, "size": int(size), "used": int(used),
                   "avail": int(avail), "target": target.strip()}
        except ValueError:
            continue
        if fstype in ("efivarfs", "tmpfs", "devtmpfs", "squashfs", "overlay") and not source.startswith("/dev/"):
            continue
        row["percent"] = round(100.0 * row["used"] / row["size"], 1) if row["size"] else 0.0
        rows.append(row)
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["source"])
        if cur is None or len(r["target"]) < len(cur["target"]):
            best[r["source"]] = r
    return sorted(best.values(), key=lambda r: r["target"])


_ROCM_PID_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s*$")


def parse_rocm_showpids(text: str) -> dict:
    """``rocm-smi --showpids`` → processes and any leading WARNING lines.

    The table columns are ``PID  PROCESS NAME  GPU(s)  VRAM USED  SDMA USED
    CU OCCUPANCY``; VRAM USED is bytes. The process name can contain spaces,
    so it is matched lazily between the PID and the GPU(s) column. When no
    process holds the device the tool prints "No KFD PIDs currently running".
    """
    procs, warnings = [], []
    in_table = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("WARNING"):
            warnings.append(s)
            continue
        if s.startswith("PID") and "PROCESS" in s:
            in_table = True
            continue
        if s.startswith("="):
            if in_table and procs:
                in_table = False
            continue
        if not in_table:
            continue
        m = _ROCM_PID_RE.match(line)
        if not m:
            continue
        procs.append({
            "pid": int(m.group(1)),
            "name": m.group(2).strip(),
            "gpus": m.group(3),
            "vram_used": int(m.group(4)),
            "sdma_used": int(m.group(5)),
            "cu_occupancy": None if m.group(6) == "UNKNOWN" else m.group(6),
        })
    return {"processes": procs, "warnings": warnings}


_SS_PROC_RE = re.compile(r'\("([^"]*)",pid=(\d+),fd=(\d+)\)')


def parse_ss_tlnp(text: str) -> list[dict]:
    """``ss -tlnp`` → listeners. One row per socket; several processes → several rows."""
    out = []
    lines = text.splitlines()
    for line in lines[1:] if lines and lines[0].startswith("State") else lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        procs = _SS_PROC_RE.findall(line)
        base = {"proto": "tcp", "addr": addr, "port": int(port) if port.isdigit() else port}
        if procs:
            for name, pid, _fd in procs:
                out.append({**base, "process": name, "pid": int(pid)})
        else:
            out.append({**base, "process": None, "pid": None})
    return sorted(out, key=lambda r: (r["port"] if isinstance(r["port"], int) else 0, r["addr"]))


def parse_ps(text: str, limit: int = 50) -> list[dict]:
    """``ps -eo pid,comm,pcpu,rss --sort=-pcpu`` → top ``limit`` rows, rss in bytes."""
    out = []
    lines = text.splitlines()
    for line in lines[1:] if lines and "PID" in lines[0] else lines:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, comm, pcpu, rss = parts
        # comm can itself contain a space (e.g. "Web Content"); ps right-aligns
        # numbers, so take pcpu/rss from the right instead.
        tail = line.split()
        try:
            out.append({"pid": int(tail[0]), "comm": " ".join(tail[1:-2]), "pcpu": float(tail[-2]),
                        "rss": int(tail[-1]) * 1024})
        except (ValueError, IndexError):
            continue
        if len(out) >= limit:
            break
    return out


def parse_loadavg(text: str) -> dict:
    p = text.split()
    running, total = (p[3].split("/") + ["0", "0"])[:2] if len(p) > 3 else ("0", "0")
    return {"load": [float(p[0]), float(p[1]), float(p[2])], "running": int(running), "total": int(total)}


def parse_stat_cpu(text: str) -> Optional[dict]:
    """First ``cpu`` line of /proc/stat → {idle, total} jiffies for a delta."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            f = [int(x) for x in line.split()[1:]]
            idle = f[3] + (f[4] if len(f) > 4 else 0)
            return {"idle": idle, "total": sum(f)}
    return None


def parse_fdinfo_drm(text: str) -> dict:
    """One /proc/PID/fdinfo/N with drm-* keys → KiB figures for gtt/vram/cpu."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(drm-(?:memory|total|resident)-(\w+)):\s*(\d+)\s*KiB", line)
        if m:
            out[m.group(1)] = int(m.group(3))
    return out


def parse_pp_dpm(text: str) -> dict:
    """``pp_dpm_sclk`` → levels and the active one (marked with ``*``)."""
    levels, active = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+):\s*(\d+)\s*M?[Hh]z\s*(\*)?", line)
        if m:
            mhz = int(m.group(2))
            levels.append(mhz)
            if m.group(3):
                active = mhz
    return {"levels_mhz": levels, "active_mhz": active}


def parse_xrt_examine(text: str) -> dict:
    """``xrt-smi examine`` → XRT version, NPU firmware, devices."""
    out: dict = {"devices": []}
    section = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "|", "-")):
            section = line.strip()
            continue
        m = re.match(r"^\s+([^:]+?)\s*:\s*(.*)$", line)
        if m and section:
            k, v = m.group(1).strip(), m.group(2).strip()
            if section == "XRT" and k == "Version":
                out["xrt_version"] = v
            elif section == "XRT" and k == "NPU Firmware Version":
                out["npu_firmware"] = v
            elif section == "XRT" and k == "amdxdna Version":
                out["amdxdna_version"] = v
        elif line.startswith("|") and section and section.startswith("Device"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and not cells[0].startswith(("BDF", "-")):
                out["devices"].append({"bdf": cells[0].strip("[]"), "name": cells[1]})
    return out


def parse_ini_preset(text: str) -> dict:
    """A llama-server router preset stanza (``[name]\\nkey = value`` lines) → dict."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([\w.-]+)\s*=\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out
