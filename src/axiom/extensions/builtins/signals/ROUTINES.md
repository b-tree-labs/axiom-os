# SCAN Routines

> OpenClaw HEARTBEAT.md equivalent — defines SCAN's continuous operational loops.

## Always-On Watchers

SCAN polls configured sources on independent intervals:

| Watcher | Interval | Description |
|---------|----------|-------------|
| **OneDrive** | 30s | Poll the consumer folder for new voice memos, documents |
| **Inbox** | 10s | Watch `runtime/inbox/raw` for files dropped by other agents or users |

## Signal Pipeline (on new input)

When a watcher detects new content:

1. Route to appropriate extractor (voice, document, image)
2. Extract structured signals (decisions, action items, corrections)
3. Match signals to existing PRD requirements
4. Stage for human review in `runtime/inbox/staged/`

## Heartbeat (30s)

- Check watcher health (source reachable, no error backlog)
- Report processing stats (items queued, processed, errored)

## On Startup

- Resume any interrupted pipeline jobs
- Verify connection health (OneDrive, GitHub, GitLab)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
