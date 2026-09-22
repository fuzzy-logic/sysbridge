# Building an app for sysbridge

*For people and for coding agents. If you are an agent, read this whole file
before writing the first line; everything an app may rely on is here.*

A sysbridge app is **one `.html` file**. No build step, no framework, no CDN.
The bridge serves it at `http://<slug>.localhost/`, hands it a token, adds a
home tab, and answers its API calls on that same origin. You write the page;
the bridge does the rest.

The quickest start is [`docs/app-template.html`](app-template.html): copy it,
change the four tags at the top, drop it on the launcher's ＋ tile.

## 1. What makes a file an app

```html
<title>My App</title>                                   <!-- tile name; slug = my-app; URL http://my-app.localhost/ -->
<meta name="description" content="One line about it.">  <!-- tile subtitle -->
<meta name="app-icon" content="🧩">                     <!-- tile icon: one emoji (else the title's initials) -->
<meta name="app-version" content="0.1">                 <!-- optional; the App Store shows it and detects updates -->
<meta name="app-author" content="you">                  <!-- optional -->
```

That is the whole manifest. The **slug** is the slugified title
(`Llama Manager` → `llama-manager`, `^[a-z0-9][a-z0-9-]{0,47}$`); it is the
folder name in the store and the host name of the app. Uploading a page with
the same title **replaces** the installed one (the old copy moves to
`.trash/`, nothing is deleted).

Rules, each with its reason:

| rule | why |
|---|---|
| Everything inline: CSS, JS, images as `data:` URIs. No `<script src>`/`<link href>` to another host. | Apps must work with no network and cannot pull code from the internet into a page that holds a token. The store check refuses external resources. |
| Call **only `location.origin`**. Never `http://localhost:8080`, `127.0.0.1:…` or any other host. | The API answers on every host, so the app follows its own origin; a hardcoded port breaks on `--port 8182`, on the `/apps/` fallback and for other users. The store check refuses hardcoded local hosts. |
| Read the token from `localStorage['sysbridge.token']` **at call time**. Send it as `X-Bridge-Token`. Never put it in a URL. | The bridge wrote it there before your first script ran. Reading at call time also covers `--no-token-inject`, where the user pastes it in the launcher. |
| Never store the **sensitive** token. If a filesystem call answers 401 the bridge is in `--fs-strict` mode: ask, keep it in a JS variable, forget it on reload. | It is the one credential the bridge never serves. |
| **Do not draw a home link.** | The bridge appends a slide-out ⌂ tab (Home, every installed app) to every app page, half off-screen at the edge the user chose in Settings. Keep that edge's centre clear of controls that must be hit precisely. Opt out only if you draw your own: `<meta name="sysbridge-home" content="none">`. |
| Poll with **one** `/v1/all?names=…` per tick, asking only for what you draw. Pause when the tab is hidden. | Each probe is cached at its own TTL; one request a second is cheap, ten are not. |
| Show `stale`. Keep the last good view when a request fails. Hide a panel entirely when its source is gone. | A dead upstream must degrade the page, never break it. Never show `NaN` or empty bars. |
| Respect `prefers-color-scheme`; use relative units. | Users run dark desktops; the reference laptop panel is 1440×900 logical pixels. |
| Keep app state in `localStorage`. Any key names. | Your origin's storage is yours alone. (On the `/apps/<slug>/` fallback apps share one origin, so the shipped apps still prefix `app:<slug>:`.) A `/v1/kv/<slug>/…` durable store is planned, not built. |

## 2. Getting the app onto the launcher

- **Upload:** drop the file on the ＋ tile at `http://localhost/`. Done.
- **The store (for sharing):** add `store/<slug>/index.html` to the repo,
  run `python -m unittest tests.test_store`, open a pull request. That test is
  the PR check; it refuses missing title/description/icon, external resources,
  hardcoded local hosts, a home link of your own, files over 4 MiB, and a
  folder name that is not the slug of the title.
- **Link tile:** a URL that opens as a tile; for UIs that are not bridge apps.

Installed files live in `~/.local/state/sysbridge/apps/<slug>/`. A fresh
install has only the App Store (the one built-in, `apps/app-store/`); every
other app, the Llama Dashboard included, is installed from the store.

## 3. The API your app talks to

Base: `location.origin + '/v1'`. Every response is JSON with
`Cache-Control: no-store`.

### Reading system data: probes

| method | path | returns |
|---|---|---|
| GET | `/v1/health` | `{ok, version, uptime_s, probes[], actions_enabled, ts}` |
| GET | `/v1/probes` | `[{name, description, ttl_ms, async_refresh, last_ok_ts, last_error}]` |
| GET | `/v1/probe/{name}` | one envelope |
| GET | `/v1/all?names=gpu,ram` | `{ok, ts, results:{name: envelope}}` — unknown names give error envelopes, still HTTP 200 |
| GET | `/v1/stream?names=…&interval_ms=1000` | Server-Sent Events, `event: probes`, data = the `/v1/all` body; interval 500–60000 ms; ≤ 4 clients |

**Envelope:** `{ok, name, ts, ttl_ms, stale, data|null, error:{type,message}|null}`.
`stale: true` = the cached value is past its TTL and the refresh failed; show
it, badge it.

| probe | ttl | `data` (bytes unless stated) |
|---|---|---|
| `gpu` | 1 s | `card, gtt{used,total}, vram{used,total}, vis_vram{…}, busy_percent, busy_note, temp_c, power_w, sclk_mhz, sclk_levels_mhz[], power_state, perf_level` — on a unified-memory APU `busy_percent` reads 100 whenever a model is resident; it is not activity |
| `cpu` | 1 s | `load[3], busy_percent (null on first sample), nproc, running, total_procs, temp_c` |
| `ram` | 1 s | `total, free, available, used, buffers, cached, shmem, swap{total,free}, zram{…}, gtt{used,total}, unified{ram_total, ram_used, gtt_used, gtt_total}` — GTT is inside `used`; draw it as an overlay |
| `disk` | 30 s | `filesystems[{source, fstype, size, used, avail, target, percent}]`, one row per device |
| `battery` | 5 s | `present, status, capacity_percent, energy_now_wh, energy_full_wh, energy_full_design_wh, health_percent, power_w, voltage_v, cycle_count, ac_online` |
| `npu` | 5 min | `present, devices_dev[], xrt_version, npu_firmware, amdxdna_version, devices[{bdf,name}]` |
| `rocm_pids` | 5 s | `processes[{pid, name, gpus, vram_used, sdma_used, cu_occupancy}], warnings[]` — the accurate per-process GPU memory |
| `kfd_holders` | 5 s | `processes[{pid, comm, fdinfo_kib{gtt,vram,cpu}}], note` — fdinfo undercounts ROCm; label it |
| `processes` | 3 s | `rows[{pid, comm, pcpu, rss}]`, top 50 by lifetime CPU average |
| `top` | 1.5 s | `uptime_s, load[3], tasks{total,running,threads}, cpu{cores[], busy_percent, nproc}, mem{total,used,available,buffers,cached,shmem,swap_total,swap_used}, processes[{pid, ppid, user, state, threads, cpu, mem, rss, comm, cmd}]` — top 200 by **instantaneous** CPU (100 = one core) |
| `ports` | 5 s | `listeners[{proto, addr, port, process, pid}]` |
| `router_models` | 30 s | `url, models[{id, status, model_path, size_bytes, shards, mmproj_path, mmproj_size_bytes, ctx_size}]` — on-disk sizes of llama-server presets, split GGUFs summed |
| `llama` | 2 s | `servers{name: {name, url, ok, mode: router|single|unknown, health, props, models[], error}}` — the llama-servers in `SYSBRIDGE_LLAMA_SERVERS`; `models[]` is the server's own `/models` `data[]` (`status.value`, `meta` when loaded) |
| `llama_slots` | 1 s | `servers{name: {ok, models{label: {slots[], error}}}}` — `slots[].is_processing` is "generating now" |

Slow probes (`rocm_pids`, `npu`, `router_models`, `llama*`, `top`) refresh
in the background after the first call, so no request of yours waits on them.

### Talking to the models

| method | path | notes |
|---|---|---|
| POST | `/v1/llama/{server}/chat` | OpenAI-style `{model, messages[{role,content}], temperature, top_p, max_tokens, stream}`; streams `text/event-stream` back (`data: {choices[0].delta.content}` … `data: [DONE]`). **Only models already loaded** are accepted (409 otherwise), so a chat can never load or evict. Fields are whitelisted; anything else is dropped. No token. ≤ 4 concurrent. |
| POST | `/v1/llama/{server}/load` · `…/unload` | `{"model": id, "confirm": true}`; token; the model must be one the router lists. Under `--models-max 1` a load **evicts** the resident model — say so in your confirm dialog, with sizes from `llama` and `router_models`. |

Never call a llama-server directly from an app. The bridge adds
`autoload=false` to every router GET (a bare `/props?model=X` would load X)
and refuses model ids the router does not list. `store/chatbridge/index.html`
is the reference for streaming; `store/llama-dashboard/index.html` for load/unload.

### Everything else

| method | path | notes |
|---|---|---|
| GET | `/v1/actions` | `[{name, description, confirm}]`; 404 = actions disabled on this machine (hide the section) |
| POST | `/v1/action/{name}` | token; `{"confirm": true}` when marked; returns `{ok, name, exit_code, stdout, stderr, ms}`. Fixed argv on the bridge; there is no way to pass arguments and your UI must not pretend otherwise |
| GET | `/v1/apps` | installed apps `[{slug, title, description, icon, kind, url, builtin, installed_at, size}]` |
| POST · DELETE | `/v1/apps`, `/v1/apps/{slug}` | install (`text/html` body, or JSON link) / uninstall; token; DELETE needs `{"confirm": true}` |
| GET · POST | `/v1/store`, `/v1/store/{slug}/install` | the catalogue and one-click install; token for POST |
| GET · PUT | `/v1/settings` | `{home_position}`; PUT needs the token; values whitelisted |
| GET | `/v1/fs/roots` · `ls?path=` · `stat?path=` · `read?path=` · `download?path=` | read-only files under configured roots; token (or the sensitive token; only that under `--fs-strict`). `store/web-file/index.html` is the reference |

Errors: `401` wrong/missing token · `403` your origin is not allowed (you are
not on localhost) or the thing is built-in/outside the roots · `400
ConfirmRequired` you forgot `{"confirm": true}` · `404` unknown name · `409`
state refuses (model not loaded, single-model server) · `429` too many streams.

## 4. Origins, tokens, and why a remote page gets nothing

- Your app runs at `http://<slug>.localhost/`. The browser gives that origin
  its own storage, so no other app can read your data or token. `*.localhost`
  resolves to this machine in Chromium, Firefox and systemd-resolved with no
  setup. `http://localhost/apps/<slug>/` is a fallback that shares the
  launcher's origin.
- The bridge prepends `<script data-sysbridge-token>` to every HTML *document*
  it serves, setting `localStorage['sysbridge.token']`. Only browser document
  requests receive it (`Sec-Fetch-Dest: document`); `fetch()` and `curl` do
  not. It is the deliberate convenience of a single-user machine;
  `--no-token-inject` restores paste-in-Settings.
- Every request is checked against an Origin allowlist: `null` (a `file://`
  page), `http://localhost`, `http://127.0.0.1`, `http://[::1]`,
  `http://<slug>.localhost`, with any port, plus `--origins` extras. Anything
  else gets **403 with no CORS headers**, so a page on the internet cannot
  read a byte even though it can reach the port. During development you may
  open your file from `file://`: it then talks to `http://localhost`.

## 5. Copy this

```js
const BRIDGE = /\.localhost$/.test(location.hostname) || location.pathname.startsWith('/apps/') ? location.origin : 'http://localhost';
const token = () => { try { return localStorage.getItem('sysbridge.token') || ''; } catch (e) { return ''; } };

async function bridgeGet(path, timeout = 3000) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(BRIDGE + path, { signal: ctl.signal, cache: 'no-store' });
    const body = await r.json().catch(() => null);
    if (!r.ok) throw new Error(body?.error?.message || `HTTP ${r.status}`);
    return body;
  } finally { clearTimeout(t); }
}
async function bridgePost(path, body) {
  const r = await fetch(BRIDGE + path, { method: 'POST', cache: 'no-store', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': token() } });
  const j = await r.json().catch(() => null);
  if (!r.ok) throw new Error(j?.error?.message || `HTTP ${r.status}`);
  return j;
}
// const all = await bridgeGet('/v1/all?names=gpu,ram'); if (!all.results.gpu.data) hideGpuPanel(); else draw(all.results.gpu);
```

Streaming a chat reply (see ChatBridge for the full version):

```js
const r = await fetch(`${BRIDGE}/v1/llama/router/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ model, messages, stream: true }) });
const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
for (;;) { const { value, done } = await rd.read(); if (done) break; buf += dec.decode(value, { stream: true });
  let i; while ((i = buf.indexOf('\n\n')) >= 0) { const ev = buf.slice(0, i); buf = buf.slice(i + 2);
    for (const line of ev.split('\n')) if (line.startsWith('data:') && line.slice(5).trim() !== '[DONE]') append(JSON.parse(line.slice(5)).choices?.[0]?.delta?.content || ''); } }
```

## 6. Before you call it done

- [ ] `<title>`, `<meta name="description">`, `<meta name="app-icon">` present; installs from the ＋ tile and appears as a tile
- [ ] Works at `http://<slug>.localhost/` **and** opened from `file://`; no console errors either way
- [ ] The network tab shows only your own origin — no direct llama-server calls, no CDN
- [ ] Bridge stopped → panels that depend on it hide or show stale; nothing throws
- [ ] Token read at call time from `localStorage`; never in a URL; sensitive token never stored
- [ ] No home link of your own; the chosen edge's centre is clear of precise controls
- [ ] Confirm dialogs name the concrete consequence, read from live state
- [ ] Nothing hardcodes `card1`, `hwmon4`, a PID, or a port
- [ ] For the store: `python -m unittest tests.test_store` passes with the file at `store/<slug>/index.html`

## 7. Reference apps

| app | shows how to |
|---|---|
| `store/llama-dashboard/index.html` | poll many probes in one request, confirm-guarded writes (load/unload), stale badges, a settings drawer |
| `apps/app-store/index.html` | token-gated POST/DELETE with clear 401 messaging |
| `store/wtop/index.html` | a dense, sortable, filterable live table at 1 s with settings kept in `localStorage` |
| `store/chatbridge/index.html` | streaming SSE through the bridge, conversations in `localStorage`, stop via `AbortController` |
| `store/web-file/index.html` | the sensitive-token fallback kept in page memory, blob downloads, previews |
| `docs/app-template.html` | the smallest complete app |
