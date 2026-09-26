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
