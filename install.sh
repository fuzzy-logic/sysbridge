#!/usr/bin/env bash
# install.sh — set up sysbridge as the user service `sysbridge-http` and open
# http://localhost/ . Idempotent; run it again after `git pull`.
#
#   ./install.sh                 install: sysctl (one sudo), unit, drop-in, enable, health check
#   ./install.sh --port 8182     skip the sysctl and serve on a high port instead
#   ./install.sh --uninstall     stop + disable, remove the symlink and drop-in; keeps your apps
#   ./install.sh --uninstall --purge-sysctl   also remove /etc/sysctl.d/80-sysbridge.conf (sudo)
#   ./install.sh --dry-run ...   print what would run, change nothing
#
# What it touches, and why each is reversible:
#   /etc/sysctl.d/80-sysbridge.conf          the ONE privileged step. A user service may
#                                            only bind ports < 1024 once
#                                            net.ipv4.ip_unprivileged_port_start is 80.
#                                            Machine-wide, all users. Reverse: delete the
#                                            file, `sysctl -w net.ipv4.ip_unprivileged_port_start=1024`.
#                                            (setcap on the python binary was rejected: it
#                                            would grant every python script the capability.)
#   ~/.config/systemd/user/sysbridge-http.service              symlink into this repo, so
#                                            `git pull` updates it. Reverse: rm the symlink.
#   ~/.config/systemd/user/sysbridge-http.service.d/local.conf  your paths (SYSBRIDGE_DIR, port).
#                                            Never overwritten once it exists; edit it by hand
#                                            or `systemctl --user edit sysbridge-http`.
#   ~/.local/state/sysbridge/                uploaded apps (StateDirectory). --uninstall leaves it.
#
# Requirements: systemd user session, python3 >= 3.9, sudo only for the sysctl step.
set -euo pipefail

UNIT=sysbridge-http
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SYSCTL_FILE=/etc/sysctl.d/80-sysbridge.conf
PORT=80
DRY=0; UNINSTALL=0; PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge-sysctl) PURGE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
# a sudo step is shown before it runs so the prompt is never a surprise
sudo_run() { printf '  sudo: %s\n' "$*"; if [ "$DRY" = 1 ]; then return 0; fi; sudo "$@"; }

# ----------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  say "Uninstalling $UNIT (your uploaded apps in ~/.local/state/sysbridge are kept)"
  if systemctl --user list-unit-files "$UNIT.service" --no-legend 2>/dev/null | grep -q "$UNIT"; then
    run systemctl --user disable --now "$UNIT" || true
  fi
  [ -L "$USER_UNITS/$UNIT.service" ] && run rm -f "$USER_UNITS/$UNIT.service"
  [ -f "$USER_UNITS/$UNIT.service.d/local.conf" ] && run rm -f "$USER_UNITS/$UNIT.service.d/local.conf"
  [ -d "$USER_UNITS/$UNIT.service.d" ] && run rmdir "$USER_UNITS/$UNIT.service.d" 2>/dev/null || true
  run systemctl --user daemon-reload
  if [ "$PURGE" = 1 ] && [ -f "$SYSCTL_FILE" ]; then
    sudo_run sh -c "rm -f '$SYSCTL_FILE' && sysctl -w net.ipv4.ip_unprivileged_port_start=1024"
  elif [ -f "$SYSCTL_FILE" ]; then
    echo "  left $SYSCTL_FILE in place (add --purge-sysctl to remove it)"
  fi
  say "Done."
  exit 0
fi

# ----------------------------------------------------------------- preflight
say "sysbridge → $UNIT on port $PORT  (repo: $REPO_DIR)"
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
python3 - <<'PY' || { echo "python3 >= 3.9 required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
systemctl --user show-environment >/dev/null 2>&1 || { echo "no systemd user session (systemctl --user failed)" >&2; exit 1; }
if ! python3 -m bridge --once cpu >/dev/null 2>&1 && [ "$DRY" = 0 ]; then
  echo "python3 -m bridge --once cpu failed from $REPO_DIR — run it yourself to see why" >&2; exit 1
fi
if ss -tln 2>/dev/null | grep -qE "[.:]$PORT\b"; then
  echo "  note: something already listens on :$PORT — the service will keep retrying until it is free" >&2
fi

# ----------------------------------------------------------------- 1. sysctl (the one sudo)
if [ "$PORT" -lt 1024 ]; then
  cur="$(sysctl -n net.ipv4.ip_unprivileged_port_start 2>/dev/null || echo 1024)"
  if [ "$cur" -le "$PORT" ] && [ -f "$SYSCTL_FILE" ]; then
    echo "  sysctl already set (unprivileged ports start at $cur) — skipping"
  else
    say "1/4  Allow user services to bind port $PORT (one-time, needs sudo)"
    echo "     writes $SYSCTL_FILE — machine-wide; reverse with --uninstall --purge-sysctl"
    sudo_run sh -c "printf 'net.ipv4.ip_unprivileged_port_start = $PORT\n' > '$SYSCTL_FILE' && sysctl -q -p '$SYSCTL_FILE'"
  fi
else
  say "1/4  Port $PORT needs no kernel change — skipping sysctl"
fi

# ----------------------------------------------------------------- 2. unit symlink
say "2/4  Unit: $USER_UNITS/$UNIT.service → repo (symlink, so git pull updates it)"
run mkdir -p "$USER_UNITS/$UNIT.service.d"
if [ -e "$USER_UNITS/$UNIT.service" ] && [ ! -L "$USER_UNITS/$UNIT.service" ]; then
  echo "  $USER_UNITS/$UNIT.service exists and is not a symlink — refusing to overwrite it" >&2; exit 1
fi
run ln -sfn "$REPO_DIR/systemd/$UNIT.service" "$USER_UNITS/$UNIT.service"

# ----------------------------------------------------------------- 3. drop-in with this machine's paths
DROPIN="$USER_UNITS/$UNIT.service.d/local.conf"
case "$REPO_DIR" in "$HOME"/*) DIR_VALUE="%h/${REPO_DIR#"$HOME"/}" ;; *) DIR_VALUE="$REPO_DIR" ;; esac
if [ -f "$DROPIN" ]; then
  say "3/4  Drop-in exists, not touching it: $DROPIN"
  grep -E '^Environment=SYSBRIDGE_(DIR|PORT)=' "$DROPIN" | sed 's/^/     /' || true
else
  say "3/4  Drop-in: $DROPIN  (SYSBRIDGE_DIR=$DIR_VALUE, SYSBRIDGE_PORT=$PORT)"
  if [ "$DRY" = 0 ]; then
    cat > "$DROPIN" <<CONF
# Written by install.sh on $(date +%F). Edit freely; install.sh never overwrites this file.
[Service]
Environment=SYSBRIDGE_DIR=$DIR_VALUE
Environment=SYSBRIDGE_PORT=$PORT
# Environment=SYSBRIDGE_PYTHON=/usr/bin/python3
# Environment=SYSBRIDGE_ROUTER_URL=http://127.0.0.1:8080
CONF
  fi
fi

# ----------------------------------------------------------------- 4. enable + check
say "4/4  systemctl --user enable --now $UNIT"
run systemctl --user daemon-reload
run systemctl --user enable --now "$UNIT"
[ "$DRY" = 1 ] && { say "Dry run complete — nothing changed."; exit 0; }

URL="http://localhost$([ "$PORT" = 80 ] || printf ':%s' "$PORT")/"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -m 2 "${URL}v1/health" >/dev/null 2>&1; then
    say "✓ $UNIT is up: $URL"
    echo "  token for installing apps / running actions:  python3 -m bridge --token"
    echo "  logs:    journalctl --user -u $UNIT -f"
    echo "  remove:  ./install.sh --uninstall"
    exit 0
  fi
  sleep 1
done
echo "✗ service started but ${URL}v1/health did not answer within 10 s:" >&2
systemctl --user --no-pager -l status "$UNIT" | sed -n '1,12p' >&2 || true
journalctl --user -u "$UNIT" -n 15 --no-pager >&2 || true
exit 1
