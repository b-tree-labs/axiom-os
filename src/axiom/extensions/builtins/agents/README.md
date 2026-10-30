<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `agents` — turning daemon agents on and off

Daemon agents (TRIAGE, TIDY, KEEP, RIVET, PRESS, …) are dispatched by one
OS-level **Background Service** (systemd timer / launchd job / scheduled
task). Two independent gates control them — both must be open for anything
to run:

| Gate | What it is | Command |
|---|---|---|
| **Autonomy** | Master switch, OFF on a fresh install (safe-by-default; 2026-07-08 directive). When off, the service ticks but dispatches nothing. | `axi settings set autonomy.enabled true` \| `false` |
| **Consent** | Which agents may persist as OS services (ADR-036; 2026-05-28 silent-registration incident). Recorded once, per host. | `axi agents register --all` \| `--agents A,B` \| `--none` |

## Turn on

```sh
axi agents info                          # what each agent does, before enabling
axi settings set autonomy.enabled true   # open the master gate FIRST
axi agents register --agents vault       # consent + install the Background Service
axi agents start
axi agents status                        # expect: running, agents listed with intervals
```

Order matters: `register` checks the autonomy gate — flipping autonomy
*after* a failed register records consent but installs nothing; just run
`register` again.

## Turn off

```sh
axi agents stop                          # stop now, service stays installed
axi settings set autonomy.enabled false  # freeze all dispatch, keep everything installed
axi agents register --agents <subset>    # drop one agent: re-consent to the rest
axi agents unregister                    # remove the OS service entirely
```

## Verify / debug

- `axi agents status` — service state, consent line, per-agent last-run.
- `axi agents logs` / journal: `journalctl --user -u neut-background-service.service`.
- Tick log: `~/.axi/agents/.background-service/ticks.jsonl` — a healthy gate
  shows `agent_count > 0`; `"skipped": "autonomy_disabled"` means the master
  switch is off. Silence ≠ healthy: check the file's mtime.

## Field notes (learned on a live node, 2026-09-01)

- **PATH**: the launch target is a venv entry point (`Axiom-Background-Service`);
  run `register` with the venv's `bin` on `PATH` or the installer refuses
  (correctly) rather than installing a unit that would 203/EXEC.
- **Timers** (pre-0.42.1): a mid-uptime install could show NEXT="-" forever
  (`OnBootSec` past, `OnUnitActiveSec` waiting on a first run). 0.42.1 adds
  `OnActiveSec` and register now verifies the timer will actually fire.
- **Consumer retire lists**: if a site ships `units.retired`, make sure it
  doesn't name `neut-background-service` — a stale entry deletes the freshly
  registered service on the next reconcile.
- Linger must be enabled on headless Linux (`loginctl enable-linger <user>`)
  or user services die with the SSH session; the installer checks and says so.

Deeper background: `docs/prds/prd-agents.md`, ADR-036 (runtime surfaces),
ADR-045 (RACI), `consent.py` docstring.

## The roster, and routing to a bound node

Each agent declares its `role`, `domain`, `namespaces`, `also_owns` and `asks`
on its `kind = "agent"` entry in its own extension's manifest. `axi agents
roster`, the `agents.roster` skill, the MCP handshake, the `[NAME]` tag on tool
descriptions, the `axiom://agents/roster` resource and the instruction-file
write-back all read that one record (`axiom.agents.roster`).

**Not built yet: asking an agent on the bound node.** `agents.ask` and
`agents.address` run the persona in this process with this process's tools.
When the client is bound to a node (ADR-207), the agent the person means is
the one on that node, which holds the node's state. The design:

1. `delegate()` asks the binding resolver (ADR-207 §2) for the bound node
   before it resolves a runner. Unbound, or a test mode, keeps the local runner.
2. Bound, a `_BoundRunner` posts `{agent, request, requester, depth}` to the
   node's agents endpoint over the binding's authenticated transport. The node
   runs the turn under its own persona and namespaces, so its authorization
   gate sees the real requester; the client never sends a persona or a toolset.
3. The answer comes back marked with the node that produced it, and the client
   relays that name with it, so a reply from a remote agent is never presented
   as local.
4. The depth limit travels in the request and the node enforces it as well,
   so a chain cannot restart its count by crossing a node.
5. A node that does not serve the endpoint returns a refusal that says so. It
   does not fall back to a local run, because a local answer would describe
   the wrong machine.

