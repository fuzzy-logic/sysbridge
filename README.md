# sysbridge

**`http://localhost/`** — one address for every local web UI you run.

sysbridge is a small localhost service that serves a home screen of
single-file `.html` apps and gives them what a browser page cannot get on its
own: system data (GPU memory, RAM, disk, CPU, battery, NPU, which processes
hold the GPU, listening ports) and an allowlist of named actions. Python 3
stdlib, zero dependencies, no build step.

It ships with one app built in, **Llama Dashboard**: what a `llama-server`
**router** is doing right now — which model is resident, who is generating,
how much GPU memory each process holds — with confirm-guarded load/unload. It
is an ordinary app: it talks only to the bridge, and the bridge talks to the
llama-servers.
Install more by uploading an `.html` file from the launcher; add tiles that
link to UIs on other ports so you never remember a port again.

```
http://localhost/                    launcher: grid of installed apps
http://localhost/apps/llama-dash/    built-in Llama Dashboard
http://localhost/apps/<slug>/        anything you upload; link tiles 302 to their URL
http://localhost/v1/...              the API every app uses (same origin, no CORS dance)
        │ GET /v1/all  POST /v1/action/…  POST /v1/apps  POST /v1/llama/…
        ▼
  sysbridge  →  /sys /proc rocm-smi xrt-smi df ss ps
             →  llama-server router :8080, reviewer :8127   (SYSBRIDGE_LLAMA_SERVERS)
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

```bash
git clone https://github.com/fuzzy-logic/sysbridge ~/ai/sysbridge
cd ~/ai/sysbridge
./install.sh
```

That is the whole install. The script:

1. **Runs the one privileged step** (asks for sudo once): writes
   `/etc/sysctl.d/80-sysbridge.conf` with `net.ipv4.ip_unprivileged_port_start = 80`
   so a user service may bind port 80 and the launcher is plain
   `http://localhost/`. This lowers the threshold for every user on the
   machine. `setcap` on the Python binary was deliberately not used; it would
   grant the capability to every Python script.
2. Symlinks `systemd/sysbridge-http.service` into `~/.config/systemd/user/`, so
   `git pull` updates it.
3. Writes a drop-in with this machine's paths (`SYSBRIDGE_DIR`, port). It never
   overwrites an existing drop-in.
4. `systemctl --user enable --now sysbridge-http`, then waits for
   `/v1/health` and prints the URL.

Nothing else. Uploaded apps live in `~/.local/state/sysbridge/apps/`
(`StateDirectory=sysbridge`).

```bash
./install.sh --dry-run                # show every step, change nothing
./install.sh --port 8182              # no sysctl, no sudo; launcher at http://localhost:8182/
./install.sh --uninstall              # stop, disable, remove symlink + drop-in; keeps your apps
./install.sh --uninstall --purge-sysctl   # also remove the sysctl file (sudo)
python -m bridge --port 8182          # or just run it in a terminal, no install at all
```

Without the sysctl, the bridge on port 80 exits with that exact instruction
rather than silently picking another port.

## Apps

**Install** = upload one `.html` from the launcher (drop it on the ＋ tile).
Conventions, no configuration:

| what | comes from |
|---|---|
| name | `<title>` (or the filename if there is none) |
| subtitle | `<meta name="description" content="…">` |
| icon | `<meta name="app-icon" content="🦙">`, else the title's initials |
| URL | `/apps/<slug>/`, slug = slugified title (`Llama Manager` → `llama-manager`) |
| replace | upload a page with the same title; the old copy moves to `.trash/` |
| uninstall | from the tile's ⋯ menu; also moves to `.trash/`, never deletes |

Every app page gets a small floating **⌂ home button** (bottom-left) added by
the bridge as it serves the file, so any upload has a way back to the launcher
with no code of its own. Apps that draw their own can opt out with
`<meta name="sysbridge-home" content="none">`.

**Link tiles** are the one thing that leaves port 80 on purpose: they open any
URL — the router's own web UI on :8080, say — so the launcher also covers UIs
that are not bridge apps. Everything uploaded is served from `/apps/<slug>/`
on the launcher's own origin.

Every app is same-origin with the API, so no app needs a bridge URL, and the
token pasted once in the launcher's Settings (`localStorage` key
`sysbridge.token`) serves every app. Apps keep their own state in
`localStorage` under `app:<slug>:…`; same-origin apps share one store, so
unprefixed keys collide. Built-in apps (`apps/<slug>/index.html` in the repo)
cannot be uninstalled.

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
| GET | `/apps` | installed apps' manifests, built-ins first |
| POST | `/apps` | install: body `text/html` = the page (≤ 4 MiB, optional `X-Filename`), or `application/json` `{"kind":"link","url",…}`; token required |
| DELETE | `/apps/{slug}` | uninstall to `.trash/`; token + `{"confirm": true}`; 403 for built-ins |
| POST | `/llama/{server}/load` `…/unload` | `{"model": id, "confirm": true}`; token; the model must be one the router lists; 409 for a single-model server |

Outside `/v1`: `/` is the launcher, `/apps/<slug>/…` serves an app's files
(slug validated, no traversal, no listings; link apps 302).

Envelope: `{ok, name, ts, ttl_ms, stale, data, error}`. `stale: true` means
the value is the last good one and the refresh failed — clients keep it and
badge it.

Probes: `gpu` `cpu` `ram` `disk` `battery` `npu` `rocm_pids` `kfd_holders`
`processes` `ports` `router_models` `llama` `llama_slots`. The last three watch
the llama-servers named in `SYSBRIDGE_LLAMA_SERVERS` (default
`router=http://127.0.0.1:8080,reviewer=http://127.0.0.1:8127`); every router
GET the bridge makes carries `autoload=false`. `python -m bridge --list` prints them with
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
- **Uploads are trusted code, by design.** An installed app runs on the bridge's origin with the same access as the launcher. Only the token holder can install; a remote page cannot (403, no CORS). Files are served with `nosniff` and only from inside the app's own directory.
- **Origin allowlist on every request:** exactly `null` (a `file://` page),
  `http://(127.0.0.1|localhost|[::1])(:port)?`, or values passed with
  `--origins`. Allowed → reflected with `Vary: Origin`. Anything else → **403
  with no CORS headers**, so a remote page cannot read a byte. No Origin (curl)
  → allowed: the gate protects the browser, not the shell.
- **Actions, installs, uninstalls and model load/unload need a token** from `$XDG_RUNTIME_DIR/sysbridge/token`
  (0600, created at start, never served). Constant-time compare.
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
./install.sh --dry-run                # what the installer would do on this machine
python -m unittest discover tests     # 64 tests: parsers, registry, apps store, llama upstreams (fake server), CORS/token API over a socket
python -m bridge --once rocm_pids     # any probe, as JSON, exit 1 on error
python -m bridge --list
python -m bridge --port 8182          # launcher at http://localhost:8182/ without the port-80 sysctl
```

Fixtures under `tests/fixtures/` are real captures (`df` with 5 btrfs
subvolume rows, `rocm-smi` with its WARNING preamble, `/proc/meminfo`,
`ss -tlnp`) with the username scrubbed.

Layout:

```
bridge/
  __main__.py   argparse: --port --bind --origins --actions-file --apps-root --once NAME --list --token
  server.py     ThreadingHTTPServer, routing, static apps, CORS, SSE, token check
  llama.py      llama-server upstreams as probes (llama, llama_slots) + load/unload
  apps.py       installed apps: manifests from <title>/<meta>, install/replace/uninstall to .trash, traversal-safe resolve
  www/launcher.html   the home screen at /
  registry.py   @probe registry, per-probe TTL cache + lock, async refresh, error isolation
  probes.py     one function per data point
  actions.py    allowlist file → fixed argv, confirm flag, mtime reload
  parsers.py    pure text → dict parsers (unit-tested)
  util.py       run(), read_sysfs(), hwmon_by_name(), first_amdgpu_card()
apps/llama-dash/index.html   built-in app (any apps/<slug>/index.html is one)
skills/sysbridge-client/SKILL.md
systemd/  config/  tests/
```

[fuzzy-logic/sysbridge](https://github.com/fuzzy-logic/sysbridge) · MIT.
