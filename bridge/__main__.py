"""python -m bridge — run the sysbridge server, or one probe, from the CLI.

  python -m bridge                       serve on 127.0.0.1:8182
  python -m bridge --once gpu            print one probe's envelope and exit
  python -m bridge --list                list probes
  python -m bridge --token               print the action token path and value
  python -m bridge --origins https://x   allow an extra exact Origin

Defaults, each with its reason:
  --port 8182     free loopback port on the reference machine (2026-09-22);
                  8080 is the llama-server router, 8127 its reviewer, 8181 is
                  what `python -m http.server` is usually given for the dashboard.
  --bind 127.0.0.1  the bridge reads /proc and runs commands as you; it must
                  never be reachable from another host. Binding elsewhere is
                  deliberately not offered.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .probes import REG
from .server import Config, serve, token_path, ensure_token


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bridge", description="sysbridge: localhost system probes + allowlisted actions")
    ap.add_argument("--port", type=int, default=8182)
    ap.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"],
                    help="loopback only; other addresses are refused on purpose")
    ap.add_argument("--origins", nargs="*", default=[], help="extra exact Origin values to allow (loopback origins and null are always allowed)")
    ap.add_argument("--actions-file", default=None, help="override the allowlist path (default ~/.config/sysbridge/actions.json)")
    ap.add_argument("--once", metavar="NAME", help="run one probe, print its envelope as JSON, exit 0/1")
    ap.add_argument("--list", action="store_true", help="list probes and exit")
    ap.add_argument("--token", action="store_true", help="print the action token (creating it if needed) and exit")
    ap.add_argument("--version", action="version", version=f"sysbridge {__version__}")
    a = ap.parse_args(argv)

    if a.list:
        for p in REG.describe():
            print(f"{p['name']:14} ttl={p['ttl_ms']:>6} ms  {p['description']}")
        return 0
    if a.once:
        env = REG.get(a.once)
        print(json.dumps(env, indent=2, sort_keys=True))
        return 0 if env["ok"] else 1
    if a.token:
        p = token_path()
        print(f"{p}\n{ensure_token(p)}")
        return 0

    cfg = Config(bind=a.bind, port=a.port, extra_origins=list(a.origins), actions_file=a.actions_file)
    try:
        serve(cfg)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
