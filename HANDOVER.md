# sysbridge — plan and handover

> **Handover note for a new session.** This file is the brief. Step 0 of the
> execution copies it to `~/ai/sysbridge/HANDOVER.md`; everything a fresh session
> needs — decisions already taken, verified facts about this machine, the API
> contract, and the order of work — is here. Facts marked *(verified 2026-09-22)*
> were measured on the machine, not assumed. Read `~/.claude/CLAUDE.md` first for
> the memory rules and the standing constraints listed at the bottom.


## Status — 2026-09-22, end of first build session

Everything below the line was built and verified in one session. The repo is
at `~/ai/sysbridge`, pushed to github.com/fuzzy-logic/sysbridge (the agent's
permission classifier blocked `gh repo create` until the user approved it in
chat; the push then went through).

| step | state | evidence |
|---|---|---|
| 0 repo + handover | done | pushed to `fuzzy-logic/sysbridge` `master`; personal-data grep clean before every push |
| 1 dashboard | done | opened from :8181: router + reviewer cards healthy, 7 presets, activity 1 s, every router GET carries `autoload=false`; confirm dialog states eviction with live sizes; cancel leaves state untouched |
| 2 bridge core | done | `--once gpu` GTT == `mem_info_gtt_used`; all 11 probes ok; `/v1/all`, `/v1/stream`, 403-no-CORS, 204 preflight verified with curl |
| 3 actions | done | unit tests: no file → 404, no/wrong token → 401, bad Origin → 403, confirm → 400, argv from request ignored |
| 4 systemd | unit + drop-in written; **not installed** | every probe passes under `systemd-run -p ProtectSystem=strict -p ProtectHome=read-only -p NoNewPrivileges=true -p PrivateTmp=true` |
| 5 skill + README | done | `skills/sysbridge-client/SKILL.md`, README in the measured-facts → design → install → dev order |

### Deviations from the plan (all additive)

- **`router_models` probe (new).** The plan wanted unloaded-model sizes "from
  the preset `model=` path + file size … only if the bridge is reachable", but
  the bridge accepts no paths from clients. So the bridge reads the router's
  own `/models`, parses each preset and stats the file server-side. It also
  **sums split GGUF shards**: `qwen3.8-flash-next`'s preset names shard 1 of 3
  (10 MiB) of a 94 GiB model. Router URL via `SYSBRIDGE_ROUTER_URL`.
- **POST bodies are drained before any early return** (401/403/404). Found by a
  test traceback: on a keep-alive connection an unread body was parsed as the
  next request line.
- **The models table only re-renders when its visible signature changes**, so
  expanders, selections and focus survive the 5 s poll.
- Dashboard also shows the reviewer's slots, sampling params and reasoning
  format per slot; sizes use `toPrecision(3)`.
- `gpu` probe carries a `busy_note`; `kfd_holders` carries the fdinfo
  undercount note; `ram` adds zram stats when present.

### Not done / open items for the user

1. **Install the unit** if wanted (reversible: symlink + drop-in, nothing copied):
   see README → Install. Not done because it enables a service.
2. **Live load/unload was not exercised** — the plan says only with the user
   present. The dialog was opened and cancelled; the POST path is exercised by
   the router's own API contract only.
3. `ports` shows process names only for the bridge's user (`ss` without root);
   documented, not worked around.
4. Optional: `pacman -S amdgpu_top` for a second opinion on GPU busy.

---

## Context

The user runs several local LLM harnesses (Pi, goose, OpenCode) against a
`llama-server` **router** on `127.0.0.1:8080` (`local-agent --serve-only`,
presets in `~/Work/models/router.ini`, `--models-max 1`) plus a dedicated
pi-cli-safe reviewer on `:8127`. Today it is opaque which model is resident, who
is calling, and what the GPU is doing; a mistaken single-model server on :8080
earlier today broke every client for an hour before anyone could see why.

Existing GUIs (LLama-GUI, llama-manager, llama-cpp-GUI) were reviewed and are
all *launchers*: they get visibility by owning the process and none reads the
router's `/models`. The user wants:

1. **Phase 1 — `apps/llama-dash/index.html`**: one file, no build, no deps,
   opened from `file://` or any localhost port, showing llama-server runtime
   state via its own API, with confirm-guarded load/unload.
2. **Phase 2 — `sysbridge`**: a small localhost service exposing read-only
   system data (GPU, RAM, disk, CPU, battery, NPU, GPU-holding processes,
   ports) *and* an allowlist of named actions that browser apps cannot reach
   themselves. Intended as a reusable base for future single-file `.html` apps,
   so the repo ships a **skill** telling an agent how to build a client.

Decisions taken by the user (do not re-open): new public repo
`github.com/fuzzy-logic/sysbridge`; dashboard has full load/unload; bridge v1
ships probes **and** allowlisted actions; Python 3 stdlib, zero dependencies.

## Verified facts that shape the design *(verified 2026-09-22)*

**llama-server API (EngramHalo.cpp fork, `build_info b1-c26c2ea`)**

- CORS reflects any `Origin`, including `null` from `file://`; methods
  `GET, POST, DELETE, OPTIONS`. A static page can call both servers directly.
- Router `GET /models` → `{data:[…]}`; per model `id`, `status.value`
  (`unloaded|loading|loaded|sleeping|downloading|failed`), `status.args[]`
  (child argv), `status.preset` (raw INI stanza), `meta` **only when loaded**
  (`n_params`, `size`, `n_ctx`, `n_ctx_train`, `n_vocab`, `ftype`),
  `architecture.input_modalities`. Single-model `:8127` returns
  `{models:[ollama-stub], data:[…]}` — **read `data[]`, tolerate both shapes**.
- Router GET endpoints take `?model=<id>`; **`/props?model=<unloaded>` triggers a
  load unless `?autoload=false`** — the dashboard must always pass it.
- `GET /slots?model=…` → per slot `is_processing`, `id_task`,
  `n_prompt_tokens`, `n_prompt_tokens_cache`, `next_token[].n_decoded`,
  `params{temperature,top_k,top_p,max_tokens,samplers[],speculative.types}`.
  This is the "who is generating right now" signal.
- `GET /props` → `model_path`, `model_alias`, `model_ftype`, `total_slots`,
  `modalities{vision,video,audio}`, `chat_template_caps{…}`, `build_info`,
  `is_sleeping`, `default_generation_settings`.
- `GET /models/sse` (router) → `text/event-stream` of `{model,event,data}` with
  `event ∈ model_status|download_progress|model_remove|models_reload`. Use it
  for live status; poll `/slots` at 1–2 s for activity.
- `POST /models/load` / `POST /models/unload` body `{"model": id}` →
  `{"success": true}`. Under `--models-max 1`, **load evicts the resident model**.
- `/metrics` is **disabled** on both servers (501, needs `--metrics`). Do not
  depend on it; enabling it is a router flag change and needs the user's approval.
- `/health`, `/props`, `/models` do not reset `--sleep-idle-seconds` (not in use).

**System data sources (no sudo)**

| data | source | notes |
|---|---|---|
| GPU GTT/VRAM, busy | `/sys/class/drm/card*/device/{mem_info_gtt_used,mem_info_gtt_total,mem_info_vram_used,mem_info_vram_total,gpu_busy_percent,pp_dpm_sclk}` | today it is `card1`, **no card0** — glob and pick the one with `mem_info_vram_total`; GTT total 96 GiB |
| GPU temp/power/clock | hwmon with `name == amdgpu`: `temp1_input` m°C, `power1_average` µW, `freq1_input` Hz | hwmon index not stable — match on `name` |
| CPU temp | hwmon `k10temp` → `Tctl` | |
| compute memory per PID | `/opt/rocm/bin/rocm-smi --showpids` (~1 s, leading WARNING line, `VRAM USED` bytes) | `fdinfo drm-memory-*` **undercounts** ROCm (6 MiB vs 25 GiB) — report it with that caveat |
| PIDs holding the GPU | `/proc/*/fd` → `/dev/kfd` | |
| RAM | `/proc/meminfo` | GTT is carved from RAM on this APU — report together |
| disk | `df -B1 --output=source,fstype,size,used,avail,target -x tmpfs -x devtmpfs` | **btrfs subvolumes repeat the same device 5×** — dedupe by `source`, shortest target |
| load | `/proc/loadavg`, `/proc/stat` delta | |
| battery | `/sys/class/power_supply/{AC,BAT0}` | |
| NPU | `/dev/accel/accel0` + `xrt-smi examine` (slow) | cache ≥ 5 min |
| processes / ports | `ps -eo pid,comm,pcpu,rss --sort=-pcpu`, `ss -tlnp` | cap 50 rows |

Tools present: `rocm-smi` (`/opt/rocm/bin`, not on PATH), `xrt-smi`, `sensors`,
`btop`, `jq`. Missing: `amd-smi`, `amdgpu_top`, `nvtop`. Free loopback ports:
**8182** (bridge), 8181, 8765. `gh auth status` works; git `user.name = Gawain`,
`init.defaultBranch = master`.

**Conventions to copy** — `~/.config/systemd/user/pi-cli-safe-laya.service`
(hardening block + `Environment=` vars with machine paths in a `.d/local.conf`
drop-in; `StateDirectory`, not `ReadWritePaths`, for state), `~/ai/pi-cli-safe`
(README order: measured facts → design → install → dev; `.gitignore` with
*reasons*; no build step), `~/.local/bin/local-agent` header style (every default
justified with a number and a date; footguns written down where they'll be read).

## Repository layout

```
~/ai/sysbridge/                       github.com/fuzzy-logic/sysbridge  (MIT)
  HANDOVER.md                         this plan, verbatim, then kept current
  README.md
  bridge/
    __init__.py  __main__.py          argparse: --port --bind --origins --once NAME --list --token
    server.py                         ThreadingHTTPServer, routing, CORS, SSE, token check
    registry.py                       @probe / @action registries, TTL cache, error isolation
    probes.py                         one decorated function per data point
    actions.py                        action runner: allowlist file → argv, confirm flag
    parsers.py                        pure string→dict parsers (unit-tested)
    util.py                           run(), read_sysfs(), hwmon_by_name(), first_amdgpu_card()
  systemd/
    sysbridge.service                 generic unit
    sysbridge.service.d/local.conf.example
  config/actions.json.example         shipped examples, disabled until copied to ~/.config
  apps/llama-dash/index.html          Phase 1 consumer
  skills/sysbridge-client/SKILL.md    how an agent builds a .html app against the bridge
  tests/                              python -m unittest: parsers, registry, server CORS/token
    fixtures/ (df-btrfs.txt, rocm-showpids.txt, meminfo.txt, ss-tlnp.txt)
  .gitignore                          __pycache__/, *.local.json, config/actions.json
```

## Phase 1 — `apps/llama-dash/index.html`

Single file, vanilla JS/CSS, dark/light via `prefers-color-scheme`, no
libraries, no `<!DOCTYPE>` tricks — a plain page anyone can open. Endpoints
configurable in a settings drawer, persisted in `localStorage`; defaults
`http://127.0.0.1:8080` (router), `http://127.0.0.1:8127` (reviewer),
`http://127.0.0.1:8182` (bridge, optional).

Panels:

1. **Servers** — one card per configured llama-server: health, `build_info`,
   `total_slots`, mode (router vs single, inferred from `/models` shape).
2. **Router models** — table from `/models`: id, status pill
   (`loaded/loading/unloaded/sleeping/failed`), size/params/ctx (from `meta`
   when loaded; from parsed `status.preset` `model=` path + file size otherwise
   *only if* the bridge is reachable — never via `/props` on an unloaded model),
   modalities, and a **Details** expander showing the preset stanza and argv.
   Live via `/models/sse`, fallback poll 5 s.
3. **Activity** — `/slots?model=<loaded>` at 1 s: generating/idle, prompt
   tokens, cache hits, tokens decoded so far, sampling params. Same for :8127.
4. **Controls (confirm-guarded)** — *Load* and *Unload* per model, `POST
   /models/load|unload`. The confirm dialog must state the consequence read from
   live state: *"Loading X will evict Y (models-max 1). Y's ~N GiB will be freed
   and X's file is M GiB; a cold reload of Y later takes 30–120 s."* Buttons
   disabled while any model is `loading`. Errors surfaced inline with the
   server's message. **No Delete** (`DELETE /models`) — it removes a model from
   the router, which is covered by the standing rule below.
5. **System (only when the bridge answers)** — GTT/VRAM bars, RAM with GTT
   overlaid, GPU busy/temp/power, top GPU-holding PIDs (rocm-smi), disk. Hidden
   entirely when :8182 is down; the page must be fully useful without it.

Also: a "last refreshed" stamp, a paused state when the tab is hidden
(`visibilitychange`), and `?autoload=false` on every router GET.

## Phase 2 — `sysbridge` service

Port **8182**, bind `127.0.0.1`. Python 3 stdlib (`http.server.ThreadingHTTPServer`,
`daemon_threads=True`). Always `application/json; charset=utf-8`,
`Cache-Control: no-store`, no `Server` header.

### API (`/v1`)

| method | path | returns |
|---|---|---|
| GET | `/v1/health` | `{ok, version, uptime_s, probes, actions_enabled, ts}` — touches no probe |
| GET | `/v1/probes` | `[{name, description, ttl_ms, last_ok_ts, last_error}]` |
| GET | `/v1/probe/{name}` | one envelope |
| GET | `/v1/all?names=gpu,ram` | `{ok, ts, results:{name: envelope}}`; unknown names → error envelopes, HTTP 200 |
| GET | `/v1/stream?names=…&interval_ms=1000` | SSE `event: probes`, same body as `/v1/all`; interval clamped 500–60000; ≤ 4 connections |
| GET | `/v1/actions` | catalogue `[{name, description, confirm}]`; 404 when no allowlist file |
| POST | `/v1/action/{name}` | runs an allowlisted argv; body `{"confirm": true}` required when the action is marked `confirm`; returns `{ok, name, exit_code, stdout, stderr, ms}` (stdout/err capped 256 KiB) |
| OPTIONS | any | 204 preflight |

Envelope: `{ok, name, ts, ttl_ms, stale, data|null, error:{type,message}|null}`.
`stale=true` = cached value past TTL and the refresh failed; clients keep the
last good value.

### Probes (`probes.py`, `@probe(name, ttl_ms, description, async_refresh=False)`)

| name | ttl_ms | source |
|---|---|---|
| `gpu` | 1000 | sysfs card glob + hwmon `amdgpu` |
| `cpu` | 1000 | `/proc/loadavg`, `/proc/stat` delta, hwmon `k10temp` |
| `ram` | 1000 | `/proc/meminfo` + GTT used/total → `unified{}` |
| `disk` | 30000 | `df` deduped by source |
| `battery` | 5000 | power_supply sysfs |
| `npu` | 300000, async | `/dev/accel/accel0` + `xrt-smi examine` |
| `rocm_pids` | 5000, async | `rocm-smi --showpids` |
| `kfd_holders` | 5000 | `/proc/*/fd` + comm + fdinfo (with undercount note) |
| `processes` | 3000 | `ps`, top 50 |
| `ports` | 5000 | `ss -tlnp` |

Per-probe lock and cache so a slow `rocm-smi` never blocks sysfs reads;
`async_refresh` probes refresh in a background thread once stale so no request
waits on a subprocess after warm-up. Every exception becomes an error envelope;
`/v1/all` is never broken by one probe. `run()` never uses `shell=True`, scrubs
env to `PATH`+`LANG=C`, applies a timeout (rocm-smi 5 s, xrt-smi 10 s, others 3 s)
and an output cap.

### Actions (`actions.py`)

- Allowlist file `~/.config/sysbridge/actions.json` (`$XDG_CONFIG_HOME` aware):
  `{"actions": {"<name>": {"argv": ["…"], "description": "…", "confirm": true}}}`.
  **Absent file = actions disabled** (`/v1/actions` → 404, POST → 404).
  Argv is fixed in the file; **no client-supplied arguments, ever, in v1**.
- Gates on every POST: Origin allowlist (below) **and** `X-Bridge-Token` equal to
  the token at `$XDG_RUNTIME_DIR/sysbridge/token` (0600, generated at start,
  printed by `python -m bridge --token`). A `file://` app obtains it by the user
  pasting it once into the app's settings; the app keeps it in `localStorage`.
  The token is never served over HTTP.
- Shipped **examples** in `config/actions.json.example` (all `confirm: true`):
  `local-agent-status` (`~/.local/bin/local-agent --status`),
  `router-models` (`curl -s http://127.0.0.1:8080/models`),
  `reviewer-restart` (`systemctl --user restart pi-cli-safe-llm`),
  `laya-restart` (`systemctl --user restart pi-cli-safe-laya`),
  `journal-reviewer` (`journalctl --user -u pi-cli-safe-llm -n 50 --no-pager`).
  **Nothing that unloads, removes or replaces a router model** — see the standing
  rule. The user copies the example into place and edits it; nothing is enabled
  by the install.
- Each action logs one line (name, exit code, ms, origin) to stderr → journald.

### Security model

- **Origin allowlist** on every request: exact `null` (file://) and
  `^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$`; `--origins` appends exact
  extras. Match → reflect in `Access-Control-Allow-Origin` + `Vary: Origin`.
  No match → **403 with no CORS headers**. No `Origin` header (curl) → allowed.
- Probe/action names validated `^[a-z0-9_]{1,32}$` and must exist in the
  registry. No paths, PIDs or fragments accepted from clients. Response cap
  1 MiB. Handler concurrency capped at 8 (`Semaphore`).
- systemd user unit copies the pi-cli-safe block — `ProtectSystem=strict`,
  `ProtectHome=read-only`, `NoNewPrivileges=true`, `PrivateTmp=true`,
  `ReadWritePaths=%t` (token dir) — but **not** `ProtectProc`/`ProcSubset`
  (breaks `/proc/*/fd`) and **not** `PrivateDevices` (breaks `/dev/kfd`,
  `rocm-smi`). If `xrt-smi` writes a cache under the read-only home, add
  `CacheDirectory=sysbridge` + `XDG_CACHE_HOME=%C`. Actions that call
  `systemctl --user` work under `NoNewPrivileges` (no privilege escalation).
- `sudo` is never available from a service or agent shell on this machine.

### `skills/sysbridge-client/SKILL.md`

A short skill for agents building future `.html` apps: the endpoint list, the
envelope, the Origin rules (why `file://` works, why a remote page cannot),
polling vs `/v1/stream`, the token paste flow for actions, a 30-line fetch
helper to copy, and the checklist (hide panels when the bridge is down; never
hardcode `card1`; show `stale`). Also documents the llama-server endpoints the
dashboard uses, so the next app doesn't rediscover `?autoload=false`.

## Order of work

**Step 0 — repo and handover.** `gh repo create fuzzy-logic/sysbridge --public`,
clone to `~/ai/sysbridge`, commit `HANDOVER.md` (this file), `README.md` skeleton,
`.gitignore`, MIT `LICENSE`. Push. Check the diff for personal data before every
push (grep for your username, email domain and hostname — machine paths belong only in
`local.conf`, never in committed files).

**Step 1 — dashboard, read-only first.** Panels 1–3 and 5 against the live
servers; verify from `file://` and from `python -m http.server 8181`. Then add
panel 4 (load/unload) with the consequence-stating confirm. Commit per panel.

**Step 2 — bridge core.** `util.py`, `parsers.py` + fixture tests,
`registry.py`, `probes.py` (`gpu`, `ram`, `cpu`, `disk`, `battery` first —
sysfs only), `server.py` with CORS + `/v1/health|probes|probe|all`. `--once gpu`
must work before the server does. Then `rocm_pids`, `kfd_holders`, `npu`,
`processes`, `ports`, `/v1/stream`.

**Step 3 — actions.** `actions.py`, token, `POST /v1/action/{name}`,
`config/actions.json.example`, tests for: no file → 404, missing token → 401,
bad Origin → 403, `confirm` required, argv never built from the request.

**Step 4 — systemd + install.** `systemd/sysbridge.service` + drop-in example;
README install steps (`ln -s` unit, `systemctl --user enable --now sysbridge`);
verify the hardening (below). Wire the dashboard's System panel to the live bridge.

**Step 5 — skill + README.** `SKILL.md`, README in the pi-cli-safe order
(what's measured, the design, install, actions, security, development).

## Verification

- **Dashboard:** open `apps/llama-dash/index.html` via `file://` → router card
  shows 7 presets with only `qwen3.6-35b-a3b` loaded; ask Pi for a completion and
  watch the Activity panel go busy/idle within 1 s; kill the reviewer (`systemctl
  --user stop pi-cli-safe-llm`) → its card turns unhealthy, page keeps working;
  start it again. Load/unload: **only with the user present**, unload the
  reviewer-equivalent test model, not the resident driver; confirm text names the
  eviction. Check the network tab: every router GET carries `autoload=false`.
- **Bridge:** `python -m bridge --once gpu` prints GTT ≈ current `mem_info_gtt_used`;
  `curl -s :8182/v1/all | python -m json.tool`; `curl -i -H 'Origin:
  https://evil.example' :8182/v1/probe/gpu` → 403 without ACAO; `curl -i -X OPTIONS
  -H 'Origin: null' -H 'Access-Control-Request-Method: GET' :8182/v1/probe/gpu` →
  204 with `Access-Control-Allow-Origin: null`; POST an action without token →
  401; with token but no `confirm` on a confirm action → 400; with both → runs.
- **Tests:** `python -m unittest discover tests` — df dedupe on the btrfs fixture
  (5 rows → 1), rocm-smi table with WARNING line and empty case, meminfo, ss,
  `hwmon_by_name` on a temp tree, registry error isolation, CORS and token via
  `http.client` on an ephemeral port.
- **Hardening:** `systemd-run --user -p ProtectSystem=strict -p
  ProtectHome=read-only -p NoNewPrivileges=true -p PrivateTmp=true -t
  /usr/bin/python3 -m bridge --once <probe>` for every probe, then
  `systemctl --user start sysbridge` + `journalctl --user -u sysbridge -f` while
  running the curl set.

## Standing constraints (from the user and `~/.claude/CLAUDE.md`)

- **Never remove a model from the router, replace the router, or bind anything
  else to :8080 without explicit approval. Adding models is safe.** Many
  harnesses name router models by id. Canonical note:
  `~/.claude/memory/tools/llama-router.md`. Consequence here: no `DELETE /models`
  in the dashboard; no shipped action that unloads or evicts; load/unload are
  user-initiated clicks behind a consequence-stating confirm.
- `sudo` cannot work from an agent shell (fingerprint auth). Ask the user to run
  privileged commands with the `!` prefix. Installing `amdgpu_top`
  (`pacman -S amdgpu_top`, in `extra`) is optional and theirs to run.
- System changes must be reversible: back up any config before editing; unit
  files are symlinked, not copied; nothing enabled without the user's say.
- No personal data in the public repo: machine paths, hostname, email stay in
  the untracked `local.conf` / `actions.json`.
- Use `git` from the first file; commit per step; run the personal-data grep
  before each push.

---

## Phase 3 — one port, a launcher at `/`, installable web apps (approved 2026-09-22)

**Why.** Remembering ports is the friction: the router is :8080, the reviewer
:8127, the bridge :8182, the dashboard wherever `http.server` was pointed.
The user wants `http://localhost/` to be the only address: a home-screen grid
of installed apps, the dashboard built in, more `.html` apps installable by
upload, and plain links for UIs that live on other ports.

**Shape.**

```
http://localhost/                 launcher: grid of installed apps (built into the bridge)
http://localhost/apps/<slug>/     an installed app; link apps 302 to their URL
http://localhost/v1/...           the existing API, unchanged
```

Every app is same-origin with the API, so no app needs to know a bridge URL
and one pasted token (shared `localStorage` key) serves the launcher and all apps.

**Conventions (no configuration).**

- Install = upload one `.html`. Name from `<title>`, subtitle from
  `<meta name="description">`, icon from `<meta name="app-icon" content="🦙">`
  (fallback: title initials). Slug = slugified title, `^[a-z0-9][a-z0-9-]{0,47}$`,
  is the URL. Same title on upload = replace.
- Link app = `{kind: "link", url, title, icon}`; a tile that opens a URL.
- Storage `$STATE_DIRECTORY/apps/<slug>/` (systemd `StateDirectory=sysbridge`),
  else `$XDG_STATE_HOME/sysbridge/apps`, else `~/.local/state/sysbridge/apps`.
  `index.html` + generated `manifest.json` (`slug, title, description, icon,
  kind, url, builtin, installed_at, size, sha256, original_filename`).
- Replace/uninstall **move** the old app to `apps/.trash/<slug>-<timestamp>/`.
  Built-ins (`llama-dash`, served from the repo) cannot be uninstalled.
- Apps keep state in `localStorage` under `app:<slug>:…` — same-origin apps
  share one store, so unprefixed keys collide. Shared on purpose:
  `sysbridge.token`, `sysbridge.url` (empty = same origin).
- Apps default their bridge URL to `location.origin` when served from `/apps/`,
  to `http://localhost` when opened from `file://`. Both keep working.

**API additions.** Same Origin gate as everything; writes need `X-Bridge-Token`.

| method | path | notes |
|---|---|---|
| GET | `/` | launcher |
| GET | `/apps/<slug>/[file]` | static, slug validated, no traversal, no listings; `/apps/<slug>` → 301 `/apps/<slug>/`; link apps → 302 |
| GET | `/v1/apps` | manifests, built-ins first |
| POST | `/v1/apps` | `Content-Type: text/html` body = the app (4 MiB cap; the launcher reads the file client-side and posts the text — Python 3.14 has no stdlib multipart parser); `application/json` body = link app |
| DELETE | `/v1/apps/<slug>` | `{"confirm": true}`; moves to trash; 403 for built-ins |

**Port 80.** Default becomes `--port 80`; on `EACCES` the bridge exits with the
fix printed rather than silently choosing another port. The one privileged
step is the user's (sudo is fingerprint-only from an agent shell):
`/etc/sysctl.d/80-sysbridge.conf` with `net.ipv4.ip_unprivileged_port_start = 80`.
It lowers the threshold machine-wide for all users; reversal = delete the file,
set 1024. `setcap` on the Python binary was rejected (grants every script).
`--port 8182` keeps working meanwhile.

**Order of work.** (1) `bridge/apps.py` + tests (slug, meta parser, install/
replace/trash/uninstall, traversal, links). (2) Static serving. (3) `/v1/apps`.
(4) Launcher page `bridge/www/launcher.html`: grid, upload (picker + drag-drop,
preview parsed title before install), add-link, per-app settings view
(manifest, uninstall). (5) llama-dash: title "Llama Dashboard", `app-icon`
meta, same-origin default, shared token key, `app:llama-dash:` prefix.
(6) Skill, README, unit (`StateDirectory`, port 80, sysctl note). (7) Browser
verification incl. remote-origin 403 on upload.

## Phase 4 — per-app command permissions (idea, not scheduled)

An app declares the CLI commands it wants (in its manifest / a meta tag); the
launcher's per-app settings view lists them and the user approves each one, or
all, per app. The bridge then exposes only approved commands to that app.
Design consequences taken *now* so this fits later: every app has a manifest
and a settings view; the token stays the write credential; the actions
allowlist remains fixed argv, so "approving a command" will mean copying a
fixed argv into an app-scoped allowlist, never passing arguments through.
A `/v1/kv/<slug>/…` storage API is the other reserved extension.
