# ADR-092: cross-mem — continuous bidirectional sync (P4 mechanisms)

**Status:** Proposed · **Date:** 2026-07-14
**Owner:** @ben
**Extends:** ADR-087 (cross-mem; D2 one import primitive, D8 write-back layer,
D10 phasing — this is P4). Resolves ADR-087 open question 2.
**Related:** ADR-088 (rag-memory corpus; vault exclusion), ADR-052
(schema-per-extension), spec-axiom-schedule (PULSE service-reliability
contract), `docs/security/cross-mem-serving-boundary.md` §4/§5.

## Context

ADR-087 D2 defines continuous sync as the one import primitive applied
continuously in both directions, hub-and-spoke, with the Axiom store the single
reconciliation point, echo suppressed by the idempotency key, running as a
managed service that is event-driven, not busy-polling. D10 defers it to P4.
Three things were left as concrete mechanism decisions, and one was an explicit
ADR-087 open question:

1. **Echo suppression mechanism.** Hub-and-spoke means Axiom writes a peer's
   memory into that peer's instruction file, and the peer's own change detector
   then reads that file back. Something must recognize *our own words* on
   read-back, or two harnesses ping-pong a fragment forever.
2. **Conflict-resolution default** (ADR-087 OQ2): last-writer-wins by event
   time vs keep-both vs confirm — undecided, and P2 OQ4 flagged that
   same-source-ref drift queues a conflict per edit, which is noisy on a
   cadence until this is decided.
3. **Write-back fallback set** (D8): AGENTS.md is the P3 primary; the survey's
   per-product rules files are named as fallbacks but were not built.
4. **Inbound secret routing** (P2 OQ6 / P3 R3): the `looks_like_secret`
   classifier exists but the importer did not route absorbed secrets to vault.

## Decision

**D1. Echo suppression is two complementary layers, both keyed on content.**
- *Marker strip (primary).* Write-back lands inside an Axiom-managed,
  marker-delimited block (`<!-- axiom:cross-mem:begin -->…end`). Change
  detection strips that region (`rendering.strip_managed_block`) before
  deriving a source change, so our block is never a candidate; a file that is
  *only* our block yields no change at all.
- *Content-hash echo index (belt-and-suspenders).* Every fragment text written
  out is recorded (`sync_echo` artifacts, keyed by content hash); any inbound
  candidate whose text hashes to one of ours is suppressed even when it arrives
  outside the markers. This is the general form of ADR-087's "the idempotency
  key recognizes a fragment we wrote out."

**D2. Conflict default: last-writer-wins by event time (resolves ADR-087 OQ2).**
The conflicting fragments stay **kept-both in the reused P2 review queue** — no
second queue, no silent loss. A durable resolution record
(`memory_conflict_resolution`) names the winner (later event time) and the
loser(s) and the policy, so the outbound path suppresses the loser while the
full pair remains human-reviewable. Event time reads in priority order: content
`event_time` (episodic) → source `imported_at` → write timestamp; ties break on
fragment id. Resolution is idempotent per conflict, so it is safe to run every
tick and across a restart. This is the streaming answer to P2 OQ4: same-source-
ref drift now resolves LWW instead of accreting unresolved conflicts.

**D3. Write-back fallbacks over the authored-file layer only (D8).** AGENTS.md
stays the primary target. P4 adds the survey's per-product rules-file fallbacks
(`.clinerules`, `.continue/rules`, `.roo/rules`, `CONVENTIONS.md`, `CLAUDE.md`,
and the rest), each reusing the P3 `InstructionFileWriteBack`: same session-
boundary / epoch-rollover cadence guard, same idempotent markered block, same
no-op-writes-nothing. Directory-style conventions map to one managed file inside
(`.roo/rules/axiom-memory.md`) so Axiom owns exactly one addressable block per
product and never fights the vendor's own auto-memory store. App-owned
databases are never written (the P2 read-only floor is unchanged).

**D4. Inbound secret routing (wires OQ6).** `import_candidates` grows an
optional `secret_detector` seam; a genuinely-new candidate whose rendered text
reads as a programmatic secret is routed to `vault` — retained but unservable
(D7) and unprojectable (ADR-088) — instead of landing as a plain fragment.
Adapter-emitted vault candidates are still refused (the P2 inbound floor holds);
this is Axiom's own classification of plaintext secrets on the way in, using the
same classifier the serving gate uses outbound. Default off → P2 behavior is
unchanged.

**D5. Managed service under the reliability contract, event-driven.** The sync
service is a service block the PULSE runner ticks — not a new daemon type. A
change trigger (detector poll or OS file-watch) durably enqueues work; the tick
dispatches it; an empty queue is a no-op, so the runner's cadence is a heartbeat,
not a busy-poll of the import path. Guarantees: `LeaseManager` single-flight;
durable pending queue + fire-log on the artifact registry; per-item ordering
apply → record-fired → dequeue, so a crash reprocesses at most once and the
content-addressed `change_id` + idempotency key land each change exactly once;
recovery re-polls and re-drains with no loss and — via D1 — no echo storm; an
injected clock is the only time source. Inbound import is continuous; instruction-
file write-back obeys the D6 hard cadence (session boundary / epoch rollover
only).

## Consequences

**Wins**
- Two harnesses stay in lock-step through the Axiom hub, both directions, with
  a single reconciliation point and no echo storm.
- The standing conflict question is closed with a default that never loses data
  and keeps every contested edit human-reviewable.
- Reuse is near-total: the P2 import primitive, dedup, and conflict queue; the
  P3 gate, rendering, cadence guard, and AGENTS.md writer; the schedule engine,
  lease, and recovery — P4 adds detection, echo, LWW resolution, the fallback
  target set, and the service block on top.

**Costs**
- Two durable record kinds (`sync_pending`, `sync_fire`) plus the echo index
  and resolution records add registry rows; all are rebuildable / prunable.
- The content-hash echo index is exact-match; paraphrase echo is caught by the
  dedup near-duplicate tier, not here (by design).

**Non-goals**
- No live cloud polling (the P2 cloud adapter stays a skeleton).
- Not a distributed multi-node service — PULSE-1 single-node lease semantics,
  as the schedule substrate provides.
- KV-cache warmth is not synced (ADR-087; out of scope by construction).

## Open questions

Tracked in `docs/working/cross-mem-p4-open-questions.md`: cross-source concurrent
edits rely on the dedup CONFLICT tier (lexical thresholds when no embedder is
pinned); the epoch-rollover *trigger* (what fires a session boundary) is a
caller event, not an in-service detector; the durable pending queue and echo
index have no retention/compaction pass yet.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
