---
name: data.adopt_watch
description: PLINTH's check on an unattended adoption copy; raises a stalled, down, mismatch or space fault, attributed to us, and is quiet when the copy is healthy or none is in progress.
---

# data.adopt_watch

Runs on the orchestrator's tick while an adoption job exists. It reads the adoption ledger and whether
the job is still being fired, and raises:

- **stalled**: the service is firing but the ledger has not advanced for an hour (`stall_after`);
- **down**: the job should be running and nothing is starting it;
- **mismatch**: a window did not verify. Highest severity. Never resolved automatically; clear it
  with `acknowledge_mismatch=<schema.table>` after investigating;
- **space**: the windows left to copy need more room than the volume has free.

Faults are delivered once through HERALD, and appear in the node's health audit and heartbeat. Every
fault is attributed to us (ADR-182).
