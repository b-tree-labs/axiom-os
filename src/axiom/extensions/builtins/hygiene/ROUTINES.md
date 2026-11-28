# TIDY Routines

> OpenClaw HEARTBEAT.md equivalent — defines TIDY's continuous operational loops.

## Always-On Heartbeat (300s interval)

TIDY runs as a persistent system service, performing these checks every cycle:

| Check | Description |
|-------|-------------|
| **Scratch sweep** | Remove expired entries, orphaned files, enforce retention policies |
| **Service health** | Validate all managed services against minimum criteria |
| **Host vitals** | CPU thermals, memory pressure, GPU state, network, power management |
| **Node audit** | Misconfiguration detection (suspend, sleep targets, governor, C-states) |
| **Repo hygiene** | Clean pycache, stale temp files, DS_Store |
| **Doc standards** | Audit `docs/` against `doc-standards.md` (`stat docs`); surface violations as findings, propose moves/renames for approval |
| **Reclaimable branches** | Surface merged branches (ancestry / content / `gh pr` MERGED) as a non-destructive INFO finding for approval; reclaim with `axi hygiene list branches --prune` |
| **Peer heartbeat** | Publish liveness to federation peers via A2A (if configured) |

## On Startup

| Check | Description |
|-------|-------------|
| **Dead-PID sweep** | Clean entries whose owning process no longer exists |
| **Infrastructure validation** | Verify K3D cluster, PostgreSQL, LLM server are healthy |
| **Misconfiguration audit** | Full node health audit (suspend, governor, kdump, rasdaemon) |
| **Journal gap analysis** | Detect hard freezes since last boot via timestamp gaps |
| **Home directory check** | Verify ownership and SSH authorized_keys integrity |

## On Exit

- Release all session-scoped scratch entries for this process


## Daily Actions storage (PULSE, 13:00 UTC)

| Check | Description |
|-------|-------------|
| **Shared storage vs cap** | Read live artifacts and the billing usage summary; HERALD alert at 70% (warn) and 85% (escalate) of the included 2 GB / 1,440 GB-hour cap |
| **Cache cap** | Alert when a repo's Actions cache is ≥80%/85% of 10 GB |
| **Policy cleanup** | Delete artifacts past their class retention (main/PR dist 3d, logs/coverage/release dist 14d, newest 2 main dists and benchmarks kept), plus merged/closed-PR caches, inside caps and never-touch. RIVET `keep` skips even past retention; `disposable` may delete early; an unreachable verdict store means retention only. `--dry-run` or `hygiene.storage_enabled=false` reports only |

The weekly digest (Monday 15:00 UTC) sends the same report through HERALD and never deletes.

## Daily GitHub hygiene (PULSE)

| Check | Description |
|-------|-------------|
| **Stale draft PRs** | `axi hygiene report drafts` — after 7 days of inactivity, post a notice comment and `tidy/stale-notice` label. After the notice expires with no activity, close bot-authored or TIDY-owned drafts; hold human-authored drafts as a Needs you approval. |
| **Notifications** | `axi hygiene report notifications` — mark threads read when the subject PR/issue is already merged or closed |
| **Packages** | `axi hygiene report packages` — inventory only; packages are never-touch and deletion is tier 2 |
| **Classify leftovers** | `axi hygiene report classify` — optional LLM pass (label-only). `approval` decisions become Needs you requests. Never logs secrets. |

`--dry-run` reports without acting. MCP stays dry-run unless `enforce` is set.

## Bare-Metal Mode

TIDY can run outside K3D as `axi hygiene install --bare` for pre-deployment monitoring.
Only requires Python + psutil. No database, no LLM.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
