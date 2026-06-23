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

## Bare-Metal Mode

TIDY can run outside K3D as `axi hygiene install --bare` for pre-deployment monitoring.
Only requires Python + psutil. No database, no LLM.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
