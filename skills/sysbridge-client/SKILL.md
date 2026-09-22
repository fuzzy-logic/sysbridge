---
name: sysbridge-client
description: Build a single-file .html app that reads system data (GPU, RAM, disk, CPU, battery, NPU, GPU-holding processes, ports) from a local sysbridge service and, optionally, runs its allowlisted actions. Also documents the llama-server router endpoints a dashboard needs. Use when asked for a local dashboard, monitor, status page or control panel that must work from file:// with no build step.
---

# Building a client for sysbridge

sysbridge is a Python-stdlib service on `http://127.0.0.1:8182` that a static
page can call directly from `file://` or any localhost port. It exists because
a browser page cannot read `/sys`, `/proc` or run `rocm-smi` itself, and every
existing GUI got its data by *owning* the process instead.

The dashboard in `apps/llama-dash/index.html` is the reference client. Copy its
patterns rather than inventing new ones.

## Rules that are not optional

1. **The page must be fully useful with the bridge down.** Poll `/v1/health`
   or just let `/v1/all` fail; hide the whole system panel on failure. Never
   show empty bars or `NaN`.
2. **Never hardcode a device.** The bridge already picked the right DRM card
   and hwmon; use what the `gpu` probe returns (`card`, `hwmon`) only for display.
3. **Show `stale`.** An envelope with `stale: true` carries the last good value
   and a fresh `error`; badge it, don't drop it.
4. **Treat `busy_percent` from the `gpu` probe as decoration.** On a unified-
   memory APU it reads 100 whenever a model is resident. Real activity comes
   from the model server (`/slots`), not the GPU.
5. **Never put the action token in a URL.** It goes in the `X-Bridge-Token`
   header, pasted once by the user into your settings drawer, kept in
   `localStorage`. The bridge never serves it.
6. **Endpoints in a settings drawer, persisted in `localStorage`, with
   defaults.** Users move ports.
7. **Pause polling when the tab is hidden** (`visibilitychange`); resume with a
   full refresh.
8. **Plain HTML file.** No bundler, no framework, no CDN. Prefer
   `prefers-color-scheme` for dark/light. Relative units — the reference
   laptop panel is only 1440×900 logical pixels.

## API (`/v1`)

| method | path | returns |
|---|---|---|
| GET | `/v1/health` | `{ok, version, uptime_s, probes[], actions_enabled, ts}` — touches no probe |
| GET | `/v1/probes` | `[{name, description, ttl_ms, async_refresh, last_ok_ts, last_error}]` |
| GET | `/v1/probe/{name}` | one envelope |
| GET | `/v1/all?names=gpu,ram` | `{ok, ts, results:{name: envelope}}`; unknown names → error envelopes, still HTTP 200 |
| GET | `/v1/stream?names=…&interval_ms=1000` | SSE `event: probes`, data = same body as `/v1/all`; interval clamped 500–60000 ms; ≤ 4 clients |
| GET | `/v1/actions` | `[{name, description, confirm}]`; **404 when no allowlist file** |
| POST | `/v1/action/{name}` | `{ok, name, exit_code, stdout, stderr, ms}`; needs `X-Bridge-Token`; `{"confirm": true}` body when `confirm` |
| OPTIONS | any | 204 preflight |

**Envelope:** `{ok, name, ts, ttl_ms, stale, data|null, error:{type,message}|null}`.

**Probes and the shape of `data`** (bytes unless stated):

| name | ttl | data |
|---|---|---|
| `gpu` | 1 s | `card, gtt{used,total}, vram{used,total}, vis_vram{…}, busy_percent, busy_note, temp_c, power_w, sclk_mhz, sclk_levels_mhz[], power_state, perf_level` |
| `cpu` | 1 s | `load[3], busy_percent (null on first sample), nproc, running, total_procs, temp_c` |
| `ram` | 1 s | `total, free, available, used, buffers, cached, shmem, swap{total,free}, zram{orig,compr,mem_used}, gtt{used,total}, unified{ram_total, ram_used, gtt_used, gtt_total, note}` — GTT is inside `used`; draw it as an overlay, not a second stack |
| `disk` | 30 s | `filesystems[{source, fstype, size, used, avail, target, percent}]` — one row per device, btrfs subvolumes already deduped |
| `battery` | 5 s | `present, status, capacity_percent, energy_now_wh, energy_full_wh, energy_full_design_wh, health_percent, power_w, voltage_v, cycle_count, ac_online` |
| `npu` | 5 min | `present, devices_dev[], xrt_version, npu_firmware, amdxdna_version, devices[{bdf,name}], xrt_ms` |
| `rocm_pids` | 5 s | `processes[{pid, name, gpus, vram_used, sdma_used, cu_occupancy}], warnings[]` — **the accurate per-process GPU memory** |
| `kfd_holders` | 5 s | `processes[{pid, comm, fdinfo_kib{gtt,vram,cpu}}], note` — who has `/dev/kfd` open; fdinfo undercounts ROCm, say so if you show it |
| `processes` | 3 s | `rows[{pid, comm, pcpu, rss}]` top 50 by CPU |
| `ports` | 5 s | `listeners[{proto, addr, port, process, pid}]` — process only for the bridge's own user |
| `router_models` | 30 s | `url, models[{id, status, model_path, size_bytes, shards, mmproj_path, mmproj_size_bytes, ctx_size}]` — on-disk sizes of llama-server router presets, split GGUFs summed |

Ask for only what you draw: `/v1/all?names=gpu,ram,cpu` is three sysfs reads;
`/v1/all` with no names also runs `rocm-smi`, `ps` and `ss`. Slow probes
(`rocm_pids`, `npu`, `router_models`) refresh in the background after the first
call, so after warm-up no request waits on a subprocess.

**Polling vs `/v1/stream`:** poll `/v1/all` at 1–2 s for a dashboard that also
polls other servers (one scheduler, one paused-state); use `/v1/stream` for a
page whose only source is the bridge. `EventSource` reconnects on its own.

## Why `file://` works and a remote page cannot

The bridge reflects the request's `Origin` only when it is exactly `null`
(what browsers send for `file://`), or matches
`http://(127.0.0.1|localhost|[::1])(:port)?`, or was passed with `--origins`.
Anything else gets **403 with no CORS headers**, so a page served from the
internet cannot read a byte even though it can reach the port. `curl` sends no
Origin and is allowed — the bridge is protecting the browser, not the shell.
Consequence for you: serve from `file://` or a localhost port; a `https://`
dev server on localhost will be refused (the regex is `http://`).

## The token paste flow for actions

1. The user runs `python -m bridge --token` (or reads
   `$XDG_RUNTIME_DIR/sysbridge/token`) and pastes it into your settings drawer.
2. Store it in `localStorage`. Send it as `X-Bridge-Token` on every POST.
3. `GET /v1/actions` needs no token; use it to build buttons. `404` = actions
   disabled on this machine — hide the section.
4. For an action with `confirm: true`, show a dialog that states the
   consequence in the user's terms, then POST `{"confirm": true}`.
5. Render `stdout`/`stderr` verbatim in a `<pre>`; `exit_code` non-zero → red.
6. `401` = wrong/missing token (point at settings); `403` = the page's Origin is
   not allowed (you are not on localhost); `400 ConfirmRequired` = you forgot 4.

Actions are fixed argv from `~/.config/sysbridge/actions.json`. **There is no
way to pass arguments and there must not be one in a client either** — no
input boxes that "add flags".

## Fetch helper to copy

```js
const BRIDGE = { url: 'http://127.0.0.1:8182', token: '' };   // from your settings drawer
async function bridgeGet(path, timeout = 3000) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(BRIDGE.url + path, { signal: ctl.signal, cache: 'no-store' });
    const body = await r.json().catch(() => null);
    if (!r.ok) throw new Error(body?.error?.message || `HTTP ${r.status}`);
    return body;
  } finally { clearTimeout(t); }
}
async function bridgeAll(names) {              // -> {ok, ts, results} or null when the bridge is down
  try { return await bridgeGet('/v1/all?names=' + names.join(',')); } catch (e) { return null; }
}
async function bridgeAction(name, confirm = true) {
  const r = await fetch(`${BRIDGE.url}/v1/action/${name}`, {
    method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': BRIDGE.token },
    body: JSON.stringify(confirm ? { confirm: true } : {}),
  });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error(body?.error?.message || `HTTP ${r.status}`);
  return body;                                  // {ok, name, exit_code, stdout, stderr, ms}
}
// usage: const all = await bridgeAll(['gpu','ram']); if (!all) hideSystemPanel(); else draw(all.results.gpu)
```

## llama-server endpoints a dashboard needs (so you don't rediscover them)

Verified on the EngramHalo.cpp fork, `build_info b1-c26c2ea`, 2026-09-22.

- **CORS**: llama-server reflects any Origin including `null`; methods
  `GET, POST, DELETE, OPTIONS`. Call it directly.
- **`GET /models`** — router: `{data:[{id, status:{value, args[], preset}, meta?, architecture{input_modalities}}]}`,
  `status.value ∈ unloaded|loading|loaded|sleeping|downloading|failed`,
  `meta` (`n_params, size, n_ctx, n_ctx_train, n_vocab, ftype`) **only when loaded**.
  Single-model server: `{models:[ollama-stub], data:[…]}` with no `status` —
  read `data[]`, infer "router" from the presence of `status.value`.
- **`?autoload=false` on every router GET.** `GET /props?model=<unloaded>`
  otherwise *loads that model*. This is the single most expensive mistake.
- **`GET /slots?model=<id>&autoload=false`** — the activity signal: per slot
  `is_processing, id_task, n_ctx, n_prompt_tokens, n_prompt_tokens_cache,
  next_token[0].n_decoded, params{temperature, top_k, top_p, min_p, max_tokens, …}`.
  400 `model is not loaded` for an unloaded model. Poll at 1 s.
- **`GET /props?model=<loaded>&autoload=false`** — `model_path, model_alias,
  total_slots, modalities{vision,video,audio}, chat_template_caps, build_info, is_sleeping`.
- **`GET /models/sse`** — `text/event-stream`, events `model_status |
  download_progress | model_remove | models_reload`. **Sends nothing until a
  status changes** — snapshot with `/models` first, use SSE only as a "refresh
  now" signal, keep a 5 s poll as fallback.
- **`POST /models/load` / `POST /models/unload`** body `{"model": id}` →
  `{"success": true}`. Under `--models-max 1` a load evicts the resident model;
  a cold load is 30–120 s. Always confirm, always say what gets evicted.
- **Never `DELETE /models`.** It removes the preset from the router and breaks
  every client that names that id. No delete button, ever.
- `/metrics` is usually disabled (501) — don't depend on it.

## Checklist before you call it done

- [ ] Opens from `file://` **and** from `python -m http.server`; no console errors either way
- [ ] Bridge stopped → system panel hidden, everything else still works
- [ ] Model server stopped → its card turns unhealthy, page keeps polling and recovers
- [ ] `stale` badge visible when a probe fails after having worked
- [ ] Every router GET carries `autoload=false` (check the network tab)
- [ ] Confirm dialog text names the concrete consequence, read from live state
- [ ] Token never appears in a URL or in the page's HTML
- [ ] Nothing hardcodes `card1`, `hwmon4` or a PID
