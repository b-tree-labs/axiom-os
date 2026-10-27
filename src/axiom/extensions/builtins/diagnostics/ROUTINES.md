# TRIAGE Routines

> OpenClaw HEARTBEAT.md equivalent — defines TRIAGE's continuous operational loops.

## Always-On Health Watch

TRIAGE runs as a background service, continuously monitoring system health:

| Check | Interval | Description |
|-------|----------|-------------|
| **Connection health** | 60s | Verify all configured connections are reachable and authenticated |
| **Agent service status** | 60s | Check that Tidy, SCAN, and PRESS services are running |
| **Configuration audit** | 300s | Check for misconfigurations, stale settings, missing dependencies |
| **Security scan** | 3600s | Audit EC content placement, log integrity, injection patterns |

## On Issue Detected

When TRIAGE detects a problem:

1. Classify severity (critical, warning, info)
2. Attempt automated diagnosis using LLM
3. If fixable by TIDY, delegate to TIDY with RACI approval
4. If not, escalate to operator with specific remediation steps

## Heartbeat (60s)

- Aggregate health status across all subsystems
- Publish summary to `axi status` output
- Log diagnostic events for audit trail

## On Startup

- Run full system diagnosis (`axi doctor` equivalent)
- Report any issues accumulated while TRIAGE was stopped
- Verify LLM gateway is available (required for diagnosis)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
