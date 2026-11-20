---
name: serve.run
description: Compose and launch the one Axiom HTTP app; --list returns the route table without binding.
allowed-tools: []
---

# serve.run

Compose every registered router into one FastAPI app and run it in a
single process (spec-serve §7). The `axi serve` CLI verb is a thin
wrapper over this skill (ADR-056).

## Params

- `host` (str, default `127.0.0.1`) — bind host.
- `port` (int, default `8787`) — bind port.
- `profile` (str, optional) — deployment profile gating which routers mount.
- `log_level` (str, default `warning`) — uvicorn log level.
- `list` (bool, default `false`) — return the composed route table
  (prefix → extension) in the result without binding a socket.
- `bound` (bool, default `false`) — run against the current site binding
  (ADR-207): refuses unless this install is bound, serves loopback only, and
  states the site, node, key's site and the node's reachability at start.
- `expose` (bool, default `false`) — with `bound`, allow a non-loopback host;
  a bound install served beyond loopback is always warned about.

## Behavior

Calls `compose_app(profile=...)` then `run_server(app, host, port)`.
`run_server` keeps the uvicorn signal-handler guard so the CLI owns
Ctrl-C / SIGTERM. With `list=true`, returns the route table and binds
nothing.
