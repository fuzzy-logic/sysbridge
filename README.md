# sysbridge

A small localhost service that gives single-file `.html` apps the system data a
browser cannot read itself — GPU memory, RAM, disk, CPU, battery, NPU, which
processes hold the GPU, listening ports — plus an allowlist of named actions.
Python 3 stdlib, zero dependencies, no build step.

It ships with its first consumer, **`apps/llama-dash/index.html`**: one file
that shows what a `llama-server` **router** is doing right now — which model is
resident, who is generating, how much GPU memory each process holds — with
confirm-guarded load/unload. Open it from `file://`. Nothing to install.

```
┌ browser page (file:// or any localhost port) ─────────────────────────────┐
│  apps/llama-dash/index.html        …or the next .html app you write       │
└───────┬──────────────────────┬─────────────────────────┬──────────────────┘
        │ GET /models /slots   │ GET /slots /props       │ GET /v1/all  POST /v1/action/…
        ▼                      ▼                         ▼
  llama-server router     llama-server (single)     sysbridge  :8182
  :8080  (your models)    :8127 (optional)          /sys /proc rocm-smi xrt-smi df ss ps
```

## What is measured, and why the design looks like this

Everything below was checked on the reference machine (AMD Strix Halo APU,
128 GB unified memory, Arch, kernel 7.1) on 2026-09-22. The full list, with
numbers, is in [HANDOVER.md](HANDOVER.md).

- **Existing llama.cpp GUIs are launchers.** They see the model because they
  started the process. None reads the router's `/models`, so none can tell you
  what a router *someone else started* is doing. This one only reads the API.
- **`GET /props?model=X` loads model X** unless `?autoload=false`. A monitoring
  page that forgets this evicts your working model. Every router GET here
  carries the flag; the skill tells the next app to do the same.
- **`gpu_busy_percent` is not activity** on a unified-memory APU — it reads
  100 % whenever a model is merely resident. `/slots` is the activity signal;
  the GPU number is shown with that caveat attached.
- **fdinfo undercounts ROCm** (6 MiB reported vs 25 GiB actually held), so
  per-process GPU memory comes from `rocm-smi --showpids` (~200 ms, cached and
  refreshed in the background), and the fdinfo figure is labelled.
- **GTT is RAM.** The iGPU has no memory of its own; the 96 GiB GTT ceiling is
  carved from the same 128 GB. The `ram` probe reports both together and the
  dashboard draws GTT as an overlay on the RAM bar, not as a second pool.
- **`df` prints a btrfs filesystem once per subvolume** (5× here). Rows are
  deduped by device.
- **`/models/sse` sends nothing until something changes.** Snapshot first, use
  the stream as a refresh signal, keep a poll as fallback.
- **A router preset names only the first shard** of a split GGUF (10 MiB of a
  94 GiB model). The `router_models` probe sums the shards.

## Install

The dashboard alone needs nothing: open `apps/llama-dash/index.html` in a
browser. Settings (top right) hold the three endpoints and persist locally.

The bridge, as a user service:

```bash
git clone https://github.com/fuzzy-logic/sysbridge ~/ai/sysbridge
cd ~/ai/sysbridge
python -m bridge --once gpu                      # works before any service exists
ln -s ~/ai/sysbridge/systemd/sysbridge.service ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/sysbridge.service.d
cp systemd/sysbridge.service.d/local.conf.example ~/.config/systemd/user/sysbridge.service.d/local.conf
$EDITOR ~/.config/systemd/user/sysbridge.service.d/local.conf   # SYSBRIDGE_DIR=%h/ai/sysbridge
systemctl --user daemon-reload
systemctl --user enable --now sysbridge
curl -s http://127.0.0.1:8182/v1/health
```

The unit is symlinked, not copied, so `git pull` updates it; machine-specific
values live in the drop-in, never in the repo. Nothing is enabled by cloning.

Ports: **8182** bridge, 8080 router, 8127 reviewer — all defaults, all
changeable in the dashboard settings and the drop-in.

## API

Base `http://127.0.0.1:8182/v1`. JSON, `Cache-Control: no-store`, no `Server`
header, responses capped at 1 MiB.

| method | path | returns |
|---|---|---|
| GET | `/health` | `{ok, version, uptime_s, probes[], actions_enabled, ts}` |
| GET | `/probes` | probe catalogue with TTLs and last errors |
| GET | `/probe/{name}` | one envelope |
| GET | `/all?names=gpu,ram` | `{ok, ts, results:{name: envelope}}` — never fails as a whole |
| GET | `/stream?names=…&interval_ms=1000` | SSE `event: probes`, same body; ≤ 4 clients |
| GET | `/actions` | `[{name, description, confirm}]`; 404 while no allowlist file exists |
| POST | `/action/{name}` | runs the fixed argv; `X-Bridge-Token` required; `{"confirm": true}` when marked |

Envelope: `{ok, name, ts, ttl_ms, stale, data, error}`. `stale: true` means
the value is the last good one and the refresh failed — clients keep it and
badge it.

Probes: `gpu` `cpu` `ram` `disk` `battery` `npu` `rocm_pids` `kfd_holders`
`processes` `ports` `router_models`. `python -m bridge --list` prints them with
descriptions; the exact `data` shapes are in
[skills/sysbridge-client/SKILL.md](skills/sysbridge-client/SKILL.md).

## Actions

Off by default. To enable:

```bash
mkdir -p ~/.config/sysbridge
cp config/actions.json.example ~/.config/sysbridge/actions.json
$EDITOR ~/.config/sysbridge/actions.json
python -m bridge --token          # paste into the dashboard's settings drawer
```

Each action is a **fixed argv**. The request body may say `{"confirm": true}`
and nothing else is read — no arguments, no substitution, in v1 or later. The
file is re-read when it changes. The shipped examples are all `confirm: true`
and none of them unloads, removes or replaces a router model: many clients name
router models by id, and taking one away breaks all of them silently.
Load/unload stay user-initiated clicks in the dashboard, behind a dialog that
says what will be evicted.

## Security model

- **Bind 127.0.0.1 only.** Other addresses are not offered.
- **Origin allowlist on every request:** exactly `null` (a `file://` page),
  `http://(127.0.0.1|localhost|[::1])(:port)?`, or values passed with
  `--origins`. Allowed → reflected with `Vary: Origin`. Anything else → **403
  with no CORS headers**, so a remote page cannot read a byte. No Origin (curl)
  → allowed: the gate protects the browser, not the shell.
- **Actions need a token** from `$XDG_RUNTIME_DIR/sysbridge/token` (0600,
  created at start, never served). Constant-time compare.
- **Nothing from the client is interpreted** beyond probe/action names
  (`^[a-z0-9_]{1,32}$`, must exist) and the `confirm` boolean.
- **systemd hardening** from pi-cli-safe: `ProtectSystem=strict`,
  `ProtectHome=read-only`, `NoNewPrivileges`, `PrivateTmp`, `MemoryMax=512M`.
  Deliberately *not* `ProtectProc`/`ProcSubset` (the `kfd_holders` probe reads
  other processes' fds) and *not* `PrivateDevices` (`rocm-smi` and `/dev/kfd`).
  Every probe was verified under those flags with `systemd-run`.
- Subprocesses: fixed argv, never a shell, env scrubbed to `PATH`+`LANG=C`,
  per-command timeout (3 s; `rocm-smi` 5 s; `xrt-smi` 10 s; actions 30 s),
  output capped at 256 KiB.

## Writing the next app

Read [skills/sysbridge-client/SKILL.md](skills/sysbridge-client/SKILL.md). It
is written for an agent: the envelope, the Origin rules, polling vs stream, the
token paste flow, a fetch helper to copy, the llama-server endpoints the
dashboard uses (including `?autoload=false`), and a done-checklist.

## Development

```bash
python -m unittest discover tests     # 39 tests: parsers on fixtures, registry, CORS/token over a socket
python -m bridge --once rocm_pids     # any probe, as JSON, exit 1 on error
python -m bridge --list
python -m http.server 8181 --directory apps/llama-dash   # the dashboard from a localhost origin
```

Fixtures under `tests/fixtures/` are real captures (`df` with 5 btrfs
subvolume rows, `rocm-smi` with its WARNING preamble, `/proc/meminfo`,
`ss -tlnp`) with the username scrubbed.

Layout:

```
bridge/
  __main__.py   argparse: --port --bind --origins --actions-file --once NAME --list --token
  server.py     ThreadingHTTPServer, routing, CORS, SSE, token check
  registry.py   @probe registry, per-probe TTL cache + lock, async refresh, error isolation
  probes.py     one function per data point
  actions.py    allowlist file → fixed argv, confirm flag, mtime reload
  parsers.py    pure text → dict parsers (unit-tested)
  util.py       run(), read_sysfs(), hwmon_by_name(), first_amdgpu_card()
apps/llama-dash/index.html
skills/sysbridge-client/SKILL.md
systemd/  config/  tests/
```

[fuzzy-logic/sysbridge](https://github.com/fuzzy-logic/sysbridge) · MIT.
