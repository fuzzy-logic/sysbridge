# sysbridge

**`http://localhost/`** — one address for every local web UI you run.

> **Agents start here.** This repo is built to be driven by a coding agent.
> - **Install sysbridge on a machine:** `git clone https://github.com/fuzzy-logic/sysbridge ~/ai/sysbridge && cd ~/ai/sysbridge && ./install.sh`
>   — one script, one sudo prompt (a sysctl so a user service may bind port 80), reversible with `./install.sh --uninstall`. Details under [Install](#install).
> - **Build an app for it:** read **[docs/BUILDING-APPS.md](docs/BUILDING-APPS.md)**, start from **[docs/app-template.html](docs/app-template.html)**,
>   put the result in `store/<slug>/index.html`, make `python -m unittest tests.test_store` pass, open a PR. Claude Code users: the same guidance is the skill
>   [skills/sysbridge-client/SKILL.md](skills/sysbridge-client/SKILL.md).
> - **Verify a change:** `python -m unittest discover tests` (97 tests, no network, no root).

sysbridge is a small localhost service that serves a home screen of
single-file `.html` apps and gives them what a browser page cannot get on its
own: system data (GPU memory, RAM, disk, CPU, battery, NPU, which processes
hold the GPU, listening ports) and an allowlist of named actions. Python 3
stdlib, zero dependencies, no build step.

A fresh install has exactly one app: the **App Store**, which installs the
apps in this repo's `store/` folder with one click. The store ships **Llama
Dashboard** (what a `llama-server` router is doing right now, with
confirm-guarded load/unload), **WTOP** (htop in a tab), **ChatBridge** (chat
with the loaded models) and **Web-File** (a read-only file browser). Anyone can
add an app with a pull request. You can also upload any `.html` from the
launcher, or add tiles that link to UIs on other ports.

Every app is served on **its own origin**, `http://<slug>.localhost/`, so the
browser isolates apps from one another — an installed app cannot read another
app's data or token.

> **Want to build an app?** Read **[docs/BUILDING-APPS.md](docs/BUILDING-APPS.md)** —
> the conventions, the full API with every probe's data shape, the token and
> origin rules, a fetch helper to copy, a done-checklist — and start from
> **[docs/app-template.html](docs/app-template.html)**. It is written for people
> **and for coding agents**: point your agent at that file and ask for the app
> you want. Agents using Claude Code get the same guidance as a skill at
> [skills/sysbridge-client/SKILL.md](skills/sysbridge-client/SKILL.md).
> To share an app, add it under `store/<slug>/` and open a pull request;
> `python -m unittest tests.test_store` is the check.

```
http://localhost/                    launcher: grid of installed apps
http://app-store.localhost/          the App Store — the only app a fresh install has
http://llama-dashboard.localhost/         Llama Dashboard, once installed from the store   (fallback: http://localhost/apps/llama-dashboard/)
http://<slug>.localhost/             anything installed or uploaded; link tiles 302 to their URL
http://<any>.localhost/v1/...        the API, answered on every host: an app calls its own origin
        │ GET /v1/all  POST /v1/action/…  POST /v1/apps  POST /v1/store/…  POST /v1/llama/…  GET /v1/fs/…
        ▼
  sysbridge (one process, port 80, routes on Host)
             →  /sys /proc rocm-smi xrt-smi df ss ps
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
| URL | `http://<slug>.localhost/`, slug = slugified title (`Llama Manager` → `llama-manager`); fallback `http://localhost/apps/<slug>/` |
| replace | upload a page with the same title; the old copy moves to `.trash/` |
| uninstall | from the tile's ⋯ menu; also moves to `.trash/`, never deletes |

Every app page gets a **home tab** added by the bridge as it serves the file:
a rounded tab that sits half off-screen, centred on one edge, showing ⌂. Hover
or focus slides it in and unfolds a menu — the launcher, the other installed
apps, token state; a tap pins it for touch. Which edge is a bridge setting,
**top** by default, changed in the launcher's Settings (`PUT /v1/settings`,
stored in `~/.local/state/sysbridge/settings.json`) or given a default with
`--home-position` / `SYSBRIDGE_HOME_POSITION`. Apps that draw their own can opt
out with `<meta name="sysbridge-home" content="none">`.

**Link tiles** are the one thing that leaves port 80 on purpose: they open any
URL, so the launcher also covers UIs that are not bridge apps.

**Origins and tokens.** Each app runs at `http://<slug>.localhost/`, its own
origin, so its storage is invisible to every other app. `*.localhost` resolves
to this machine in Chromium, Firefox and systemd-resolved with no setup; the
path form `/apps/<slug>/` remains as a fallback and shares the launcher's
origin. **The bridge writes the token into every app's `localStorage` as it
serves the page**: a one-line `<script>` prepended to each HTML document
(launcher and apps), so `localStorage['sysbridge.token']` is set before the
app's first script runs. Nothing to paste, nothing in URLs. Only browser
document requests receive it (`Sec-Fetch-Dest: document`, or `Accept:
text/html` from older browsers); `curl` and `fetch()` do not. The trade-off,
chosen deliberately for a single-user machine: a local process that fetches an
app page with browser headers can read the ordinary token. Remote web pages
cannot, because a cross-origin document is unreadable to them. Run with
`--no-token-inject` (or `SYSBRIDGE_TOKEN_INJECT=0` in the drop-in) to go back
to pasting it in the launcher's Settings. The API answers on every host, so an
app always calls `location.origin`.

There are **two tokens**, both 0600 under `$XDG_RUNTIME_DIR/sysbridge/`:
`token`, which the bridge injects into pages as above and which opens
everything, the file browser included; and `token-sensitive`, which is
**never** injected or served. By default the filesystem API accepts either.
Start the bridge with `--fs-strict` (or `SYSBRIDGE_FS_STRICT=1`) and only the
sensitive token opens files; Web-File then asks for it each session and keeps
it in page memory. `python -m bridge --token` prints both.

**The App Store.** `store/<slug>/index.html` in the repo is the catalogue.
The built-in App Store app lists it with install/update/uninstall; `GET /v1/store`
is the data. To add an app, open a pull request adding one folder whose name
is the slug of the app's `<title>`. `python -m unittest tests.test_store` is
the PR check: title, description and icon present, one file under 4 MiB, no
external script/stylesheet/media, no hardcoded `localhost:port` (use
`location.origin`), no home link of its own (the bridge injects one).

| store app | what | needs |
|---|---|---|
| **Llama Dashboard** | which model is resident on the llama-server router, who is generating, GPU memory per process; confirm-guarded load/unload that names what gets evicted | `llama`, `llama_slots`, `router_models` probes; `POST /v1/llama/<server>/load|unload` |
| **WTOP** | htop in a tab: per-core CPU, memory, uptime, load, every process with instantaneous CPU and RSS. Read-only. | `top` probe |
| **ChatBridge** | chat with the models loaded right now; streaming; conversations stay in the browser | `POST /v1/llama/<server>/chat` — loaded models only, so a chat can never load or evict |
| **Web-File** | read-only file browser and previewer under the allowed roots (your home by default) | `GET /v1/fs/…`; roots in `~/.config/sysbridge/fs.json`; `--fs-strict` for a separate token |

A terminal app was considered and deliberately **not** built: the service runs
with `ProtectHome=read-only`, so a shell inside it could not do real work, and
a shell is the one feature where a single bug is total compromise. If it comes,
it will be a separate opt-in unit on its own origin with the sensitive token.

The only built-in app (`apps/app-store/index.html`) cannot be uninstalled;
everything else can, and comes back from the store in one click.

## API

Base `http://localhost/v1` — and the same on every `http://<slug>.localhost/`
host, so an app always calls `location.origin`. JSON, `Cache-Control:
no-store`, no `Server` header, responses capped at 1 MiB. The full reference
with every probe's `data` shape is in
[docs/BUILDING-APPS.md](docs/BUILDING-APPS.md#3-the-api-your-app-talks-to).

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
| POST | `/llama/{server}/chat` | OpenAI-style `{model, messages, temperature, top_p, max_tokens, stream}` streamed through; **loaded models only**; fields whitelisted; no token; ≤ 4 concurrent |
| GET | `/store` · `/store/{slug}` | catalogue with `installed`, `update_available`, `problems` |
| POST | `/store/{slug}/install` | token; refuses entries with `problems` |
| GET / PUT | `/settings` | bridge-wide settings; PUT needs the token; keys and values whitelisted (`home_position`: top/left/bottom/right) |
| GET | `/fs/roots` · `/fs/ls?path=` · `/fs/stat?path=` · `/fs/read?path=` · `/fs/download?path=` | token (or the sensitive one; only that with `--fs-strict`); read-only; paths must resolve inside a configured root |

Outside `/v1`: on `localhost` `/` is the launcher and `/apps/<slug>/…` an app;
on `<slug>.localhost` `/…` is that app's files (validated slug, no traversal,
no listings; link apps 302). Every HTML app page gets the home menu appended.

Envelope: `{ok, name, ts, ttl_ms, stale, data, error}`. `stale: true` means
the value is the last good one and the refresh failed — clients keep it and
badge it.

Probes: `gpu` `cpu` `ram` `disk` `battery` `npu` `rocm_pids` `kfd_holders`
`processes` `ports` `router_models` `llama` `llama_slots` `top`. The last three watch
the llama-servers named in `SYSBRIDGE_LLAMA_SERVERS` (default
`router=http://127.0.0.1:8080,reviewer=http://127.0.0.1:8127`); every router
GET the bridge makes carries `autoload=false`. `python -m bridge --list` prints them with
descriptions; the exact `data` shapes are in
[docs/BUILDING-APPS.md](docs/BUILDING-APPS.md).

## Actions

Off by default. To enable:

```bash
mkdir -p ~/.config/sysbridge
cp config/actions.json.example ~/.config/sysbridge/actions.json
$EDITOR ~/.config/sysbridge/actions.json
python -m bridge --token          # only needed with --no-token-inject; apps normally get the token from the bridge
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
- **Per-app origins are the trust boundary.** An installed app runs on `http://<slug>.localhost/`, isolated by the browser from the launcher and from every other app. It holds only the ordinary token, which the bridge writes into its storage when serving the page. Only the token holder can install; a remote page cannot (403, no CORS). Files are served with `nosniff` and only from inside the app's own directory.
- **The ordinary token is embedded in served pages** (browser document requests only) and, by default, opens the file browser too. That is a deliberate convenience for a single-user machine; `--no-token-inject` and `--fs-strict` are the two dials back towards caution. The sensitive token is never served.
- **The filesystem is the one place a client-supplied path is accepted.** It is fenced by `realpath` containment inside configured roots and is read-only. It opens with the ordinary token by default; `--fs-strict` demands the separate sensitive token that is never served.
- **Origin allowlist on every request:** exactly `null` (a `file://` page),
  `http://(127.0.0.1|localhost|[::1])(:port)?`, the per-app origins
  `http://<slug>.localhost(:port)?`, or values passed with `--origins`. Allowed → reflected with `Vary: Origin`. Anything else → **403
  with no CORS headers**, so a remote page cannot read a byte. No Origin (curl)
  → allowed: the gate protects the browser, not the shell.
- **Actions, installs, uninstalls and model load/unload need the ordinary token** from `$XDG_RUNTIME_DIR/sysbridge/token`
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

**[docs/BUILDING-APPS.md](docs/BUILDING-APPS.md)** is the guide, for people and
for agents: what makes a file an app, the rules and why each exists, the whole
API with data shapes, origins and tokens, code to copy, a checklist, and which
shipped app to read for which pattern. **[docs/app-template.html](docs/app-template.html)**
is the smallest complete app. For Claude Code the same guidance is packaged as
the skill [skills/sysbridge-client/SKILL.md](skills/sysbridge-client/SKILL.md).

A good agent prompt is simply: *"Read docs/BUILDING-APPS.md, then build a
sysbridge app that ⟨does X⟩; put it in store/⟨slug⟩/index.html and make
`python -m unittest tests.test_store` pass."*

## Development

```bash
./install.sh --dry-run                # what the installer would do on this machine
python -m unittest discover tests     # 97 tests: parsers, registry, apps, store catalogue (the PR check), top, fs, llama upstreams (fake server), CORS/tokens/per-app origins over a socket
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
  __main__.py   argparse: --port --bind --origins --actions-file --apps-root --home-position --no-token-inject --fs-strict --once NAME --list --token
  server.py     ThreadingHTTPServer, routing, static apps, CORS, SSE, token check
  llama.py      llama-server upstreams as probes (llama, llama_slots) + load/unload + streaming chat proxy
  top.py        the `top` probe: instantaneous per-process CPU from /proc deltas
  store.py      the store catalogue, validate() (the PR check), install
  fs.py         read-only filesystem under configured roots
  settings.py   bridge-wide settings with whitelisted keys/values (home tab position)
  apps.py       installed apps: manifests from <title>/<meta>, install/replace/uninstall to .trash, traversal-safe resolve
  www/launcher.html   the home screen at /
  registry.py   @probe registry, per-probe TTL cache + lock, async refresh, error isolation
  probes.py     one function per data point
  actions.py    allowlist file → fixed argv, confirm flag, mtime reload
  parsers.py    pure text → dict parsers (unit-tested)
  util.py       run(), read_sysfs(), hwmon_by_name(), first_amdgpu_card()
apps/app-store/                  the one built-in app (any apps/<slug>/index.html would be one)
store/llama-dashboard/  store/wtop/  store/chatbridge/  store/web-file/   the catalogue (PRs add folders)
docs/BUILDING-APPS.md  docs/app-template.html   how to build an app (people + agents), and the starter
skills/sysbridge-client/SKILL.md               the same, packaged as a Claude Code skill
systemd/  config/  tests/
```

[fuzzy-logic/sysbridge](https://github.com/fuzzy-logic/sysbridge) · MIT.
