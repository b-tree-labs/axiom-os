# Neut Sense Health Monitoring — Design Plan

**Status:** Draft for Review  
**Created:** 2026-02-24  
**Owner:** Ben Lindley

---

## Executive Summary

The Sense system processes signals from multiple channels (voice memos, Teams transcripts, GitHub, publisher, etc.) and publishes synthesized outputs to various endpoints (PRDs, changelogs, briefings). Currently, there's no unified view of system health—whether channels are active, if pipelines are functioning, or what actions are recommended.

This plan proposes a **Sense Health** subsystem that provides:
1. Real-time visibility into signal source activity and health
2. Publication endpoint status with recency tracking
3. End-to-end pipeline monitoring
4. Queryable health data for Neut (agent-assisted diagnostics)
5. Suggested follow-up actions (for both Neut and human operators)
6. Visual System Status dashboard for authorized users

---

## Problem Statement

### Current Pain Points

| Issue | Impact |
|-------|--------|
| No visibility into channel activity | Don't know if voice memos are syncing, Teams transcripts flowing |
| Publication staleness undetected | PRDs may be out of sync for weeks without notification |
| Pipeline failures silent | Transcription errors, LLM failures go unnoticed |
| Manual health checks | Must run multiple commands to understand system state |
| No recommended actions | Operator must diagnose issues independently |

### User Needs

**For Neut (Agent)**:
- Query health data: "What's the status of my signal sources?"
- Get suggested actions: "publisher sync is 14 days stale → run `neut doc pull --all`"
- Reference explicit health information when answering questions

**For Human Operators (System Status View)**:
- At-a-glance dashboard of all signal sources
- Visual indicators (heatmaps, sparklines) of activity levels
- Publication endpoint freshness
- Actionable recommendations
- Drill-down into specific channels

---

## Architecture

### Health Data Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                    SENSE HEALTH SUBSYSTEM                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐   │
│  │ Channel Health  │   │ Pipeline Health │   │ Endpoint Health │   │
│  │                 │   │                 │   │                 │   │
│  │ • Signal Source │   │ • Ingest→Signal │   │ • Publications  │   │
│  │ • Activity Rate │   │ • Signal→Synth  │   │ • Last Update   │   │
│  │ • Last Received │   │ • Synth→Publish │   │ • Sync Status   │   │
│  │ • Error Count   │   │ • Latency       │   │ • Access Health │   │
│  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘   │
│           │                     │                     │             │
│           └─────────────────────┼─────────────────────┘             │
│                                 ▼                                    │
│                    ┌─────────────────────┐                          │
│                    │   Health Store      │                          │
│                    │   (JSON/SQLite)     │                          │
│                    └──────────┬──────────┘                          │
│                               │                                      │
│           ┌───────────────────┼───────────────────┐                 │
│           ▼                   ▼                   ▼                 │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐       │
│  │  Neut Query     │ │  CLI Commands   │ │  System Status  │       │
│  │  Interface      │ │  (neut health)  │ │  Web Dashboard  │       │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘       │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Health Metrics by Category

#### 1. Signal Sources (Channels)

| Channel | Metrics Tracked | Health Indicators |
|---------|-----------------|-------------------|
| **Voice Memos** | Files received, transcription success rate, avg latency | Last upload, pending count |
| **Teams Chat** | Messages extracted, API connection, sync cursor | Last sync, error rate |
| **GitHub** | Events polled, issues/PRs processed | API rate limit, last poll |
| **GitLab** | Export files processed, signal count | Last export, backlog |
| **publisher** | Comments extracted, documents tracked | Sync staleness, error count |
| **Calendar** | Events synced, upcoming meetings | Last sync, coverage window |
| **Freetext/Notes** | Files ingested, signals extracted | Pending files, parse errors |

#### 2. Processing Pipelines

| Pipeline | Stages | Health Checks |
|----------|--------|---------------|
| **Voice→Signal** | Upload → Transcribe → Correct → Extract | Stage completion rate, avg duration |
| **Teams→Signal** | Sync → Parse → Extract → Correlate | Success rate, error types |
| **Signal→Synthesis** | Cluster → Draft → Approve → Publish | Queue depth, approval backlog |
| **publisher→Local** | Pull → Extract → Diff → Update | Sync success, conflict count |

#### 3. Publication Endpoints

| Endpoint | Metrics | Health Indicators |
|----------|---------|-------------------|
| **PRD Documents** | Last update, source signals | Days since update, pending signals |
| **Changelogs** | Generated, approved, published | Approval backlog, freshness |
| **OneDrive/O365** | Sync status, file mapping | Connection health, staleness |
| **Briefings** | Generated, delivered | Request backlog, delivery rate |

---

## Data Schema

### Health State File (`inbox/state/health.json`)

```json
{
  "version": "1.0",
  "last_updated": "2026-02-24T15:30:00Z",
  "channels": {
    "voice": {
      "status": "healthy",
      "last_activity": "2026-02-24T14:00:00Z",
      "pending_count": 0,
      "recent_errors": [],
      "metrics": {
        "files_24h": 3,
        "transcription_success_rate": 1.0,
        "avg_latency_seconds": 45
      }
    },
    "teams_chat": {
      "status": "stale",
      "last_activity": "2026-02-10T09:00:00Z",
      "pending_count": 0,
      "recent_errors": ["API token expired"],
      "metrics": {
        "messages_24h": 0,
        "last_sync_attempt": "2026-02-24T10:00:00Z"
      }
    },
    "publisher": {
      "status": "degraded",
      "last_activity": "2026-02-18T00:00:00Z",
      "pending_count": 4,
      "recent_errors": [],
      "metrics": {
        "tracked_docs": 1,
        "pending_pull": 4,
        "days_since_sync": 6
      }
    }
  },
  "pipelines": {
    "voice_to_signal": {
      "status": "healthy",
      "last_run": "2026-02-24T14:05:00Z",
      "success_rate_7d": 0.95,
      "avg_duration_seconds": 120
    }
  },
  "endpoints": {
    "ops-log-prd": {
      "status": "stale",
      "last_updated": "2026-02-10T00:00:00Z",
      "pending_signals": 12,
      "external_sync": "unknown"
    }
  },
  "suggested_actions": [
    {
      "priority": "high",
      "category": "sync",
      "action": "neut doc pull --all",
      "reason": "publisher sync is 6 days stale with 4 pending documents",
      "can_auto_execute": true
    },
    {
      "priority": "medium",
      "category": "auth",
      "action": "Renew Teams API token",
      "reason": "Teams chat sync failing due to expired token",
      "can_auto_execute": false
    }
  ]
}
```

---

## CLI Interface

### Commands

```bash
# Overall health summary
neut health
neut health --json              # Machine-readable output
neut health --verbose           # Detailed breakdown

# Channel-specific
neut health channels            # All channels
neut health channels voice      # Specific channel
neut health channels --stale    # Only show stale/degraded

# Pipeline status
neut health pipelines           # E2E pipeline status
neut health pipelines voice_to_signal

# Endpoint status
neut health endpoints           # Publication endpoints
neut health endpoints --stale   # Only stale endpoints

# Suggested actions
neut health actions             # List recommended actions
neut health actions --execute   # Auto-execute safe actions
neut health actions --priority high  # Filter by priority
```

### Example Output

```
$ neut health

╭─ Sense System Health ─────────────────────────────────────────────╮
│                                                                    │
│  Overall: ⚠️  DEGRADED (2 issues require attention)                │
│                                                                    │
├─ Channels ─────────────────────────────────────────────────────────┤
│  ✓ voice          3 signals/24h      last: 2h ago                 │
│  ✓ github         12 events/24h      last: 30m ago                │
│  ⚠ teams_chat     0 signals/24h      last: 14d ago   [TOKEN EXPIRED]
│  ⚠ publisher        0 signals/24h      last: 6d ago    [4 PENDING]  │
│  ○ calendar       not configured                                   │
│                                                                    │
├─ Pipelines ────────────────────────────────────────────────────────┤
│  ✓ voice→signal           95% success    avg: 2m                  │
│  ✓ signal→synthesis       100% success   avg: 30s                 │
│  ⚠ publisher→local          0% success     last: 6d ago             │
│                                                                    │
├─ Endpoints ────────────────────────────────────────────────────────┤
│  ⚠ ops-log-prd    stale (14d)   12 pending signals        │
│  ⚠ experiment-manager-prd stale (14d)   8 pending signals         │
│  ✓ publisher-spec           fresh (6d)    0 pending                 │
│                                                                    │
├─ Suggested Actions ────────────────────────────────────────────────┤
│  HIGH   Run `neut doc pull --all` — publisher 6d stale (auto-fix)   │
│  MEDIUM Renew Teams API token — auth expired                       │
│  LOW    Configure calendar sync — enable for meeting context       │
│                                                                    │
╰────────────────────────────────────────────────────────────────────╯

Run `neut health actions --execute` to auto-fix HIGH priority items.
```

---

## Neut Query Interface

For agent-based interaction, health data is queryable:

### Structured Queries

```python
from tools.pipelines.sense.health import HealthStore

health = HealthStore()

# Get overall status
status = health.get_status()
# → {"overall": "degraded", "channels": {...}, "actions": [...]}

# Get stale channels
stale = health.get_stale_channels(threshold_days=7)
# → ["teams_chat", "publisher"]

# Get suggested actions
actions = health.get_actions(priority="high", auto_executable=True)
# → [{"action": "neut doc pull --all", ...}]

# Natural language summary for Neut
summary = health.get_summary_for_agent()
# → "Sense system is degraded. publisher sync is 6 days stale..."
```

### MCP Tool Integration

```python
@mcp.tool()
async def sense_health_status() -> str:
    """Get current Sense system health status for agent queries."""
    health = HealthStore()
    return health.get_summary_for_agent()

@mcp.tool()
async def sense_suggested_actions() -> list[dict]:
    """Get recommended actions for Sense system health."""
    health = HealthStore()
    return health.get_actions()
```

---

## System Status Web Dashboard

### UI Components

#### 1. Overview Panel
- **Overall Health Indicator**: Green/Yellow/Red with status message
- **Last Updated**: Timestamp with auto-refresh option
- **Quick Stats**: Channels active, signals/24h, pending actions

#### 2. Channel Activity Heatmap
```
        Mon   Tue   Wed   Thu   Fri   Sat   Sun
voice   ██    ███   █     ████  ██          
teams   ███   ███   ███   ███   ███         
github  █     ██    ███   █████ ████  ██    █
publisher                   █                 
```
- 7-day rolling view
- Intensity = signal volume
- Click to drill down

#### 3. Endpoint Freshness Chart
```
Endpoint                    │ Freshness │ Pending │
────────────────────────────┼───────────┼─────────┤
ops-log-prd         │ ████████░░│ 12      │ 14d stale
experiment-manager-prd      │ ████████░░│ 8       │ 14d stale
publisher-spec               │ ██████████│ 0       │ fresh
axiom-executive-prd    │ ███░░░░░░░│ 5       │ 21d stale
```

#### 4. Pipeline Sparklines
```
voice→signal:    ▁▂▃▅▇█▇▅▃▂▁ (95% success, 2m avg)
signal→synth:    █████████████ (100% success, 30s avg)
publisher→local:   ▁▁▁▁▁▁▁▁▁▁▁ (0% - stale)
```

#### 5. Actions Panel
- Priority-sorted list
- "Execute" button for auto-fixable items
- Link to detailed diagnostics

### Technical Implementation

- **Framework**: Serve from existing `neut signal serve` infrastructure
- **Route**: `/system-status` (requires auth in production)
- **Data**: Fetch from `/api/health` endpoint
- **Refresh**: WebSocket or 30s polling
- **Export**: JSON download for offline analysis

---

## Health Collection Mechanisms

### Passive Collection (Existing Touchpoints)

Each existing command updates health state:

| Command | Health Updates |
|---------|----------------|
| `neut signal ingest` | Channel activity, pipeline success |
| `neut signal synthesize` | Pipeline completion, endpoint updates |
| `neut doc publish` | Endpoint freshness |
| `neut doc pull` | publisher sync status |
| `neut signal corrections` | Correction pipeline status |

### Active Probes (New)

```bash
# Run health probes (scheduled or manual)
neut health probe
```

Probes:
- **API connectivity**: Test Teams, GitHub, O365 API access
- **Credential validity**: Check token expiration
- **External sync**: Compare local vs. remote doc timestamps
- **Disk usage**: Check inbox/processed sizes

### Scheduled Health Updates

```yaml
# config/health.yaml
probes:
  api_connectivity:
    schedule: "0 */6 * * *"  # Every 6 hours
    channels: [teams_chat, github, publisher]
  
  credential_check:
    schedule: "0 8 * * *"    # Daily at 8am
    alert_days_before_expiry: 7
  
  external_sync:
    schedule: "0 */12 * * *" # Every 12 hours
    check: [publisher, calendar]
```

---

## Suggested Actions Framework

### Action Categories

| Category | Examples | Auto-Executable |
|----------|----------|-----------------|
| **sync** | `neut doc pull --all`, `neut signal ingest` | Yes |
| **auth** | Renew API token, re-authenticate | No (requires human) |
| **config** | Add missing channel config | No |
| **maintenance** | Cleanup old files, compact DB | Yes (with confirmation) |
| **escalation** | Contact admin, review errors | No |

### Action Generation Logic

```python
def generate_actions(health: HealthState) -> list[SuggestedAction]:
    actions = []
    
    # publisher staleness
    if health.channels.publisher.days_since_sync > 7:
        actions.append(SuggestedAction(
            priority="high",
            category="sync",
            action="neut doc pull --all",
            reason=f"publisher sync is {health.channels.publisher.days_since_sync}d stale",
            can_auto_execute=True,
        ))
    
    # Pending signals for stale endpoints
    for endpoint, data in health.endpoints.items():
        if data.pending_signals > 10 and data.days_stale > 7:
            actions.append(SuggestedAction(
                priority="medium",
                category="sync",
                action=f"neut signal synthesize --target {endpoint}",
                reason=f"{data.pending_signals} signals pending for {endpoint}",
                can_auto_execute=True,
            ))
    
    # Auth errors
    for channel, data in health.channels.items():
        if "token expired" in str(data.recent_errors).lower():
            actions.append(SuggestedAction(
                priority="high",
                category="auth",
                action=f"Renew {channel} API credentials",
                reason="Authentication token expired",
                can_auto_execute=False,
            ))
    
    return sorted(actions, key=lambda a: PRIORITY_ORDER[a.priority])
```

---

## Implementation Phases

### Phase 1: Health Store & CLI (1 week)
- [ ] Create `HealthStore` class with JSON persistence
- [ ] Add health update hooks to existing commands
- [ ] Implement `neut health` CLI command
- [ ] Basic suggested actions generation

### Phase 2: Neut Integration (3 days)
- [ ] MCP tools for health queries
- [ ] Agent-friendly summary generation
- [ ] Action execution via agent commands

### Phase 3: Web Dashboard (1 week)
- [ ] `/system-status` route in serve.py
- [ ] Channel activity heatmap
- [ ] Endpoint freshness chart
- [ ] Actions panel with execute buttons

### Phase 4: Active Probes (3 days)
- [ ] API connectivity probes
- [ ] Credential validity checks
- [ ] Scheduled probe execution
- [ ] Alert on degradation

---

## Files to Create/Modify

### New Files
```
tools/pipelines/sense/health.py           # HealthStore, metrics collection
tools/pipelines/sense/health_probes.py    # Active health probes
tools/agents/config/health.yaml        # Probe schedules, thresholds
inbox/state/health.json                # Health state persistence
```

### Modified Files
```
tools/pipelines/sense/cli.py              # Add `neut health` commands
tools/pipelines/sense/serve.py            # Add /system-status, /api/health
tools/pipelines/sense/correlator.py       # Health update on ingest
tools/publisher/engine.py                # Health update on publish/pull
tools/mcp_server/server.py             # Add health MCP tools
```

---

## Open Questions

1. **Alert Channels**: Should health degradation trigger notifications (Slack, email)?
2. **History Retention**: How long to keep health history for trend analysis?
3. **Multi-user**: In team scenarios, whose health view is authoritative?
4. **Thresholds**: What defines "stale" vs. "degraded" vs. "critical"?
5. **Dashboard Auth**: How to restrict System Status view to authorized users?

---

## Success Criteria

- [ ] `neut health` provides accurate real-time summary
- [ ] Neut can answer "What's the status of my signal sources?"
- [ ] Suggested actions correctly identify common issues
- [ ] Auto-executable actions work safely
- [ ] System Status dashboard renders correctly
- [ ] Health updates happen passively (no extra commands needed)

---

## Related Documents

- [Signal Sense & Synthesis Synthesis MVP Spec](../tech-specs/signal-synthesis-mvp-spec.md)
- [Agent State Management PRD](../prd/agent-state-management-prd.md) — Retention policies
- [publisher Specification](../tech-specs/publisher-spec.md) — Publication endpoints
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
