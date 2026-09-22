"""One decorated function per data point. Each returns plain JSON data.

Sources and their quirks were measured on a unified-memory AMD APU with
ROCm; see HANDOVER.md. Every
probe degrades to an error envelope rather than guessing when a source is
missing, so the same code runs on a machine with no GPU at all.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.request
from typing import Optional

from . import llama, parsers, top as _top
from .registry import Registry
from .util import first_amdgpu_card, hwmon_by_name, read_int, read_sysfs, read_text, run

REG = Registry()
probe = REG.probe

# The llama-server router whose presets the dashboard wants file sizes for:
# the upstream named "router" in SYSBRIDGE_LLAMA_SERVERS (else the first one).
ROUTER_URL = llama.SERVERS.get("router") or next(iter(llama.SERVERS.values()), "http://127.0.0.1:8080")


# ------------------------------------------------------------------ gpu
@probe("gpu", ttl_ms=1000, description="amdgpu memory (GTT/VRAM), busy %, temperature, power, clock from sysfs + hwmon")
def gpu() -> dict:
    card = first_amdgpu_card()
    if card is None:
        raise FileNotFoundError("no DRM card with mem_info_vram_total under /sys/class/drm")
    mem = read_sysfs(card, ["mem_info_gtt_used", "mem_info_gtt_total", "mem_info_vram_used", "mem_info_vram_total",
                            "mem_info_vis_vram_used", "mem_info_vis_vram_total"])
    out: dict = {
        "card": os.path.basename(os.path.dirname(card)),
        "gtt": {"used": mem.get("mem_info_gtt_used"), "total": mem.get("mem_info_gtt_total")},
        "vram": {"used": mem.get("mem_info_vram_used"), "total": mem.get("mem_info_vram_total")},
        "vis_vram": {"used": mem.get("mem_info_vis_vram_used"), "total": mem.get("mem_info_vis_vram_total")},
        "busy_percent": read_int(os.path.join(card, "gpu_busy_percent")),
        "busy_note": "on a unified-memory APU busy% reads 100 whenever a model is resident; use the server's /slots for activity",
        "power_state": read_text(os.path.join(card, "power_dpm_state")),
        "perf_level": read_text(os.path.join(card, "power_dpm_force_performance_level")),
    }
    dpm = read_text(os.path.join(card, "pp_dpm_sclk"))
    if dpm:
        sclk = parsers.parse_pp_dpm(dpm)
        out["sclk_levels_mhz"] = sclk["levels_mhz"]
        out["sclk_mhz"] = sclk["active_mhz"]
    hw = hwmon_by_name("amdgpu")
    if hw:
        t = read_int(os.path.join(hw, "temp1_input"))
        p = read_int(os.path.join(hw, "power1_average")) or read_int(os.path.join(hw, "power1_input"))
        f = read_int(os.path.join(hw, "freq1_input"))
        out["temp_c"] = t / 1000.0 if t is not None else None
        out["power_w"] = p / 1e6 if p is not None else None
        if f is not None:
            out["sclk_mhz"] = out.get("sclk_mhz") or f // 1_000_000
        out["hwmon"] = os.path.basename(hw)
    return out


# ------------------------------------------------------------------ cpu
_cpu_prev: dict = {}


@probe("cpu", ttl_ms=1000, description="load average, busy % since last sample, k10temp Tctl")
def cpu() -> dict:
    la = parsers.parse_loadavg(read_text("/proc/loadavg", "0 0 0 0/0 0"))
    st = parsers.parse_stat_cpu(read_text("/proc/stat", ""))
    busy = None
    if st:
        prev = _cpu_prev.get("stat")
        if prev and st["total"] > prev["total"]:
            dt, di = st["total"] - prev["total"], st["idle"] - prev["idle"]
            busy = round(100.0 * (1.0 - di / dt), 1)
        _cpu_prev["stat"] = st
    out = {"load": la["load"], "running": la["running"], "total_procs": la["total"],
           "nproc": os.cpu_count(), "busy_percent": busy}
    hw = hwmon_by_name("k10temp")
    if hw:
        # Tctl is the first temp on k10temp; check the label in case that ever changes.
        for i in range(1, 8):
            if read_text(os.path.join(hw, f"temp{i}_label")) == "Tctl":
                t = read_int(os.path.join(hw, f"temp{i}_input"))
                out["temp_c"] = t / 1000.0 if t is not None else None
                break
    return out


# ------------------------------------------------------------------ ram
@probe("ram", ttl_ms=1000, description="/proc/meminfo plus GTT, because GTT is carved from the same RAM on an APU")
def ram() -> dict:
    mi = parsers.parse_meminfo(read_text("/proc/meminfo", ""))
    total, free, avail = mi.get("MemTotal", 0), mi.get("MemFree", 0), mi.get("MemAvailable", 0)
    out = {
        "total": total, "free": free, "available": avail, "used": total - avail,
        "buffers": mi.get("Buffers"), "cached": mi.get("Cached"), "shmem": mi.get("Shmem"),
        "swap": {"total": mi.get("SwapTotal", 0), "free": mi.get("SwapFree", 0)},
        "zram": None,
    }
    card = first_amdgpu_card()
    if card:
        g = read_sysfs(card, ["mem_info_gtt_used", "mem_info_gtt_total"])
        out["gtt"] = {"used": g.get("mem_info_gtt_used"), "total": g.get("mem_info_gtt_total")}
        out["unified"] = {
            "ram_total": total,
            "ram_used": total - avail,
            "gtt_used": g.get("mem_info_gtt_used"),
            "gtt_total": g.get("mem_info_gtt_total"),
            "note": "GTT pages are ordinary RAM pinned for the GPU; they are already inside ram_used",
        }
    zs = glob.glob("/sys/block/zram*/mm_stat")
    if zs:
        try:
            f = read_text(zs[0], "").split()
            out["zram"] = {"orig": int(f[0]), "compr": int(f[1]), "mem_used": int(f[2])}
        except (ValueError, IndexError):
            pass
    return out


# ------------------------------------------------------------------ disk
@probe("disk", ttl_ms=30000, description="df, one row per device (btrfs subvolumes deduped)")
def disk() -> dict:
    r = run(["df", "-B1", "--output=source,fstype,size,used,avail,target", "-x", "tmpfs", "-x", "devtmpfs"], timeout=3)
    if r["exit_code"] != 0 and not r["stdout"]:
        raise RuntimeError(f"df exit {r['exit_code']}: {r['stderr'].strip()}")
    return {"filesystems": parsers.parse_df(r["stdout"])}


# ------------------------------------------------------------------ battery
@probe("battery", ttl_ms=5000, description="power_supply sysfs: battery charge, power draw, AC state")
def battery() -> dict:
    root = "/sys/class/power_supply"
    out: dict = {"present": False, "ac_online": None}
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        typ = read_text(os.path.join(d, "type"))
        if typ == "Mains":
            out["ac_online"] = bool(read_int(os.path.join(d, "online"), 0))
        elif typ == "Battery" and not out["present"]:
            v = read_sysfs(d, ["capacity", "energy_now", "energy_full", "energy_full_design", "power_now",
                               "voltage_now", "cycle_count", "charge_now", "charge_full", "current_now"])
            out.update({
                "present": bool(read_int(os.path.join(d, "present"), 1)),
                "name": os.path.basename(d),
                "status": read_text(os.path.join(d, "status")),
                "capacity_percent": v.get("capacity"),
                "energy_now_wh": v.get("energy_now", 0) / 1e6 if "energy_now" in v else None,
                "energy_full_wh": v.get("energy_full", 0) / 1e6 if "energy_full" in v else None,
                "energy_full_design_wh": v.get("energy_full_design", 0) / 1e6 if "energy_full_design" in v else None,
                "power_w": v.get("power_now", 0) / 1e6 if "power_now" in v else None,
                "voltage_v": v.get("voltage_now", 0) / 1e6 if "voltage_now" in v else None,
                "cycle_count": v.get("cycle_count"),
            })
            if out["energy_full_wh"] and out["energy_full_design_wh"]:
                out["health_percent"] = round(100.0 * out["energy_full_wh"] / out["energy_full_design_wh"], 1)
    return out


# ------------------------------------------------------------------ npu
@probe("npu", ttl_ms=300000, description="XDNA NPU presence and xrt-smi examine (slow; cached 5 min)", async_refresh=True)
def npu() -> dict:
    dev = sorted(glob.glob("/dev/accel/accel*"))
    out: dict = {"present": bool(dev), "devices_dev": dev}
    if not dev:
        return out
    r = run(["xrt-smi", "examine"], timeout=10)
    out["xrt_ms"] = r["ms"]
    if r["exit_code"] != 0 and not r["stdout"]:
        out["error"] = r["stderr"].strip()[:500]
        return out
    out.update(parsers.parse_xrt_examine(r["stdout"]))
    return out


# ------------------------------------------------------------------ rocm_pids
@probe("rocm_pids", ttl_ms=5000, description="rocm-smi --showpids: compute memory per process (the accurate figure)", async_refresh=True)
def rocm_pids() -> dict:
    r = run(["rocm-smi", "--showpids"], timeout=5)
    if r["exit_code"] != 0 and not r["stdout"]:
        raise RuntimeError(f"rocm-smi exit {r['exit_code']}: {r['stderr'].strip()[:300]}")
    out = parsers.parse_rocm_showpids(r["stdout"] + "\n" + r["stderr"])
    out["ms"] = r["ms"]
    return out


# ------------------------------------------------------------------ kfd_holders
@probe("kfd_holders", ttl_ms=5000, description="processes holding /dev/kfd, with fdinfo drm-memory (undercounts ROCm allocations)")
def kfd_holders() -> dict:
    procs = []
    for pdir in glob.glob("/proc/[0-9]*"):
        try:
            fds = os.listdir(os.path.join(pdir, "fd"))
        except OSError:
            continue
        holds = False
        for fd in fds:
            try:
                if os.readlink(os.path.join(pdir, "fd", fd)) == "/dev/kfd":
                    holds = True
                    break
            except OSError:
                continue
        if not holds:
            continue
        pid = int(os.path.basename(pdir))
        mem: dict = {}
        for fd in fds:
            txt = read_text(os.path.join(pdir, "fdinfo", fd))
            if txt and "drm-driver:" in txt:
                d = parsers.parse_fdinfo_drm(txt)
                if d:
                    # several fds map the same DRM client; take the max per key rather than summing
                    for k, v in d.items():
                        mem[k] = max(mem.get(k, 0), v)
        procs.append({
            "pid": pid,
            "comm": read_text(os.path.join(pdir, "comm")),
            "fdinfo_kib": {"gtt": mem.get("drm-memory-gtt"), "vram": mem.get("drm-memory-vram"), "cpu": mem.get("drm-memory-cpu")},
        })
    return {"processes": sorted(procs, key=lambda p: p["pid"]),
            "note": "fdinfo drm-memory-* undercounts ROCm/HIP allocations by orders of magnitude; use rocm_pids for the real number"}


# ------------------------------------------------------------------ processes
@probe("processes", ttl_ms=3000, description="top 50 processes by CPU (ps -eo pid,comm,pcpu,rss)")
def processes() -> dict:
    r = run(["ps", "-eo", "pid,comm,pcpu,rss", "--sort=-pcpu"], timeout=3)
    return {"rows": parsers.parse_ps(r["stdout"], limit=50)}


# ------------------------------------------------------------------ ports
@probe("ports", ttl_ms=5000, description="listening TCP sockets (ss -tlnp); process names only for the bridge's own user")
def ports() -> dict:
    r = run(["ss", "-tlnp"], timeout=3)
    return {"listeners": parsers.parse_ss_tlnp(r["stdout"])[:50]}


# ------------------------------------------------------------------ router_models
@probe("router_models", ttl_ms=30000, description="llama-server router presets with on-disk file sizes (sizes are unavailable over the router API until a model is loaded)", async_refresh=True)
def router_models() -> dict:
    """Read the router's /models and stat each preset's model file.

    The router only reports size/params in ``meta`` once a model is loaded,
    and asking ``/props`` for an unloaded model would load it. The bridge can
    stat the file instead. Paths come from the router's own preset text, never
    from the client.
    """
    req = urllib.request.Request(ROUTER_URL.rstrip("/") + "/models?autoload=false", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 — fixed loopback URL from config
        body = json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    models = []
    for m in body.get("data", []) or []:
        st = m.get("status") or {}
        preset = parsers.parse_ini_preset(st.get("preset") or "")
        size, shards = gguf_size(preset.get("model"))
        row = {"id": m.get("id"), "status": st.get("value"), "model_path": preset.get("model"),
               "mmproj_path": preset.get("mmproj"), "ctx_size": _int_or_none(preset.get("ctx-size")),
               "size_bytes": size, "shards": shards, "mmproj_size_bytes": gguf_size(preset.get("mmproj"))[0]}
        models.append(row)
    return {"url": ROUTER_URL, "models": models}


_SHARD_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")


def gguf_size(path: Optional[str]) -> tuple[Optional[int], int]:
    """(total bytes, shard count) for a GGUF path; split files are summed.

    A router preset names only the first shard (``…-00001-of-00003.gguf``),
    which can be a few megabytes of a model that is tens of gigabytes, while
    llama-server mmaps all of them. Reporting the first shard alone would be
    badly misleading.
    """
    if not path:
        return None, 0
    path = os.path.expanduser(path)
    m = _SHARD_RE.match(os.path.basename(path))
    if not m:
        try:
            return os.stat(path).st_size, 1
        except OSError:
            return None, 0
    stem, _, n = m.group(1), m.group(2), int(m.group(3))
    total, found = 0, 0
    for i in range(1, n + 1):
        try:
            total += os.stat(os.path.join(os.path.dirname(path), f"{stem}-{i:05d}-of-{n:05d}.gguf")).st_size
            found += 1
        except OSError:
            continue
    return (total if found else None), found


def _int_or_none(s: Optional[str]) -> Optional[int]:
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


# llama-server upstreams as probes (llama, llama_slots) — see llama.py
llama.register(REG)
# htop's view of the machine — see top.py
_top.register(REG)
