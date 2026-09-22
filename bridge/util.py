"""Small helpers shared by probes and actions.

Everything here is deliberately boring: read a sysfs file, find a hwmon by
name, run a fixed argv with a timeout and a scrubbed environment. No shell,
no client-supplied strings ever reach these functions.
"""
from __future__ import annotations

import glob
import os
import subprocess
import time
from typing import Iterable, Optional

# subprocess environment: PATH only (plus LANG=C so parsers see stable output).
# rocm-smi lives in /opt/rocm/bin, which is not on a service's PATH.
_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/rocm/bin"
_ENV = {"PATH": _PATH, "LANG": "C", "LC_ALL": "C"}

OUTPUT_CAP = 256 * 1024  # bytes of stdout/stderr kept from any subprocess


class RunError(RuntimeError):
    pass


def run(argv: list[str], timeout: float = 3.0, cap: int = OUTPUT_CAP) -> dict:
    """Run ``argv`` (never a shell string) and return exit code, stdout, stderr, ms.

    Output is capped at ``cap`` bytes per stream. Raises RunError when the
    binary is missing or the timeout fires, so callers can turn it into an
    error envelope.
    """
    if not argv or not isinstance(argv, (list, tuple)) or not all(isinstance(a, str) for a in argv):
        raise RunError("argv must be a non-empty list of strings")
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            list(argv), capture_output=True, timeout=timeout, env=_ENV,
            stdin=subprocess.DEVNULL, check=False,
        )
    except FileNotFoundError:
        raise RunError(f"{argv[0]}: not found on PATH ({_PATH})")
    except subprocess.TimeoutExpired:
        raise RunError(f"{argv[0]}: timed out after {timeout:g}s")
    ms = int((time.monotonic() - t0) * 1000)
    return {
        "exit_code": p.returncode,
        "stdout": p.stdout[:cap].decode("utf-8", "replace"),
        "stderr": p.stderr[:cap].decode("utf-8", "replace"),
        "ms": ms,
        "truncated": len(p.stdout) > cap or len(p.stderr) > cap,
    }


def read_text(path: str, default: Optional[str] = None) -> Optional[str]:
    """Read a small text file (sysfs/procfs); ``default`` when missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path: str, default: Optional[int] = None) -> Optional[int]:
    s = read_text(path)
    if s is None:
        return default
    try:
        return int(s.split()[0])
    except (ValueError, IndexError):
        return default


def read_sysfs(base: str, names: Iterable[str]) -> dict:
    """Read several attributes under ``base``; missing ones are omitted."""
    out = {}
    for n in names:
        v = read_int(os.path.join(base, n))
        if v is not None:
            out[n] = v
    return out


def hwmon_by_name(name: str, root: str = "/sys/class/hwmon") -> Optional[str]:
    """Path of the hwmon whose ``name`` file equals ``name``.

    hwmon indices are not stable across boots (amdgpu was hwmon4 on
    2026-09-22 and can move), so always match on the name file.
    """
    for d in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        if read_text(os.path.join(d, "name")) == name:
            return d
    return None


def first_amdgpu_card(root: str = "/sys/class/drm") -> Optional[str]:
    """Device dir of the first DRM card that exposes ``mem_info_vram_total``.

    Never hardcode ``card0``: on the reference machine the iGPU is ``card1``
    and there is no ``card0`` at all. Connector dirs (card1-eDP-1, …) have no
    ``device/mem_info_*`` so the glob skips them naturally.
    """
    for d in sorted(glob.glob(os.path.join(root, "card[0-9]*", "device"))):
        if os.path.exists(os.path.join(d, "mem_info_vram_total")):
            return d
    return None


def xdg_config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def xdg_runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
