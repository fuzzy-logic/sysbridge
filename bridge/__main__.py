"""python -m bridge — run the sysbridge server, or one probe, from the CLI.

  python -m bridge                       serve http://localhost/ (port 80; see README for the one-time sysctl)
  python -m bridge --port 8182           serve on a high port, no setup
  python -m bridge --once gpu            print one probe's envelope and exit
  python -m bridge --list                list probes
  python -m bridge --token               print the action token path and value
  python -m bridge --origins https://x   allow an extra exact Origin

Defaults, each with its reason:
  --port 80       so the launcher is http://localhost/ — the whole point is not
                  remembering ports. Needs net.ipv4.ip_unprivileged_port_start=80
                  (README → Install). 8182 was the pre-launcher default and is
                  still free on the reference machine (8080 router, 8127 reviewer).
  --bind 127.0.0.1  the bridge reads /proc and runs commands as you; it must
                  never be reachable from another host. Binding elsewhere is
                  deliberately not offered.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .probes import REG
from .server import Config, serve, token_path, sensitive_token_path, ensure_token


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bridge", description="sysbridge: localhost system probes + allowlisted actions")
    ap.add_argument("--port", type=int, default=80, help="80 so the launcher is just http://localhost/ (needs the sysctl in the README); 8182 for dev")
    ap.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"],
                    help="loopback only; other addresses are refused on purpose")
    ap.add_argument("--origins", nargs="*", default=[], help="extra exact Origin values to allow (loopback origins and null are always allowed)")
    ap.add_argument("--actions-file", default=None, help="override the allowlist path (default ~/.config/sysbridge/actions.json)")
    ap.add_argument("--no-token-inject", action="store_true",
                    help="do not write the ordinary token into served pages' localStorage; users paste it in the launcher instead")
    ap.add_argument("--fs-strict", action="store_true",
                    help="filesystem API accepts only the separate token-sensitive (never injected into pages); Web-File then prompts for it")
    ap.add_argument("--home-position", default=os.environ.get("SYSBRIDGE_HOME_POSITION", "top"), choices=["top", "left", "bottom", "right"],
                    help="default edge for the slide-out home tab in apps; the launcher's Settings can change it at runtime")
    ap.add_argument("--apps-root", default=None, help="where uploaded apps live (default $STATE_DIRECTORY/apps or ~/.local/state/sysbridge/apps)")
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
        p, q = token_path(), sensitive_token_path()
        print(f"token            {ensure_token(p)}   ({p})\n"
              f"token-sensitive  {ensure_token(q)}   ({q})  — only needed with --fs-strict; never injected into pages")
        return 0

    cfg = Config(bind=a.bind, port=a.port, extra_origins=list(a.origins), actions_file=a.actions_file, apps_root=a.apps_root,
                 inject_token=not (a.no_token_inject or os.environ.get("SYSBRIDGE_TOKEN_INJECT", "1") == "0"),
                 fs_strict=a.fs_strict or os.environ.get("SYSBRIDGE_FS_STRICT", "0") == "1", home_position=a.home_position)
    try:
        serve(cfg)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
