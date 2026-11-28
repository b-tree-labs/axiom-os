# ADR-282: History belongs to one agent

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-163, ADR-241, ADR-242, ADR-243; chunk [C17](https://github.com/b-tree-labs/axiom-os-private/issues/2331) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

Receipts, guard verdicts, attest records and delivery receipts live in separate stores with no one agent that answers 'what happened, in what order'.

## Decision (adopted Nov 24 2026, to be proven)

- **CLERK owns history.** CLERK answers for the backward 'when' through history skills over the existing stores, which stay lowercase libraries. attest records move to CLERK. KEEL operates node-local log sinks and defers retention drops to CLERK. WARDEN reads security evidence through history and may place a hold; it cannot edit or delete history.

## Consequences

- C17 (CLERK answers for history) delivers it. Scope: history skills (timeline, query, retention, export, attestation lookup, hold) over the existing stores; attest moves to CLERK's namespace; KEEL's retention drops ask history.retention; WARDEN gets history.hold.
- Retired in the same chunk: attest under guard's namespace; the duplicate query front doors listed in section 13.

## Proof

- C17: pytest tests/conformance/test_history_timeline.py: for one guarded action, history.timeline returns the skill call, the guard verdict, the attest record and the delivery receipt in order, each verified; a held record cannot be dropped.
