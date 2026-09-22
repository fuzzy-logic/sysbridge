# sysbridge

A small localhost service that gives single-file `.html` apps read-only system
data (GPU, RAM, disk, CPU, battery, NPU, GPU-holding processes, ports) and an
allowlist of named actions — plus `apps/llama-dash`, a one-file dashboard for a
`llama-server` router.

Python 3 stdlib, zero dependencies, no build step.

Work in progress. The plan and the verified facts behind it are in
[HANDOVER.md](HANDOVER.md).

## Layout

```
bridge/                 the service (python -m bridge)
apps/llama-dash/        index.html — llama-server router dashboard
config/                 actions.json.example
systemd/                user unit + drop-in example
skills/sysbridge-client SKILL.md — how an agent builds a client
tests/                  python -m unittest discover tests
```

[fuzzy-logic/sysbridge](https://github.com/fuzzy-logic/sysbridge) · MIT.
