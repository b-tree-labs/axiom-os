# Axiom Memory Compaction — Technical Specification

**Status:** Draft (normative for Axiom 0.17+; **stages 4–6 of the maturation lifecycle** — see `spec-memory-maturation.md`)
**Owner:** Ben Booth
**Created:** 2026-05-12
**Authority:** Normative contract for *compaction* — the compressive operation that summarizes, archives, and eventually forgets fragments under retention policy. Owns the producer side of the tombstone-event channel that `spec-memory-reflection.md §10` consumes. TIDY (hygiene agent) is the principal operator.
**Position in the lifecycle:** Stages 4 (summarize), 5 (archive), 6 (forget). Sits after stage 3 (consolidate) and ahead of the substrate's end-of-life.
**PRD:** `docs/prds/prd-memory.md` (parent — substrate)
**Related:**
- `spec-memory-maturation.md` — the umbrella; lifecycle frame this spec slots into
- `spec-memory-reflection.md` — stage 3; consumes tombstone events this spec produces
- `spec-memory.md` — substrate (write path, MIRIX taxonomy, tombstone semantics)
- `spec-classification-boundary.md` — per-regime retention windows
- `prd-agents.md` — TIDY owns compaction + archive + retention; WARDEN audits retention; SCAN monitors stage lag
- ADR-026 (ownership; cryptographic erasure), ADR-027 (federated memory + cohort cold tier)

---

## Quick Start — what 95% of extension authors need

The default maturation profile runs compaction across every scope. Most extensions need nothing.

When you *do* want to customize, the three knobs:

```toml
[maturation."my-extension-scope:*".compaction]
# Summarize episodes older than N (lossy: writes a summary fragment, marks
# original as superseded). Default 7d for `default` profile.
summarize_age = "7d"

# Tombstone superseded originals older than N (the original content is
# gone from hot storage). Default 30d for `default` profile.
tombstone_age = "30d"

# Move warm/cold per the tier policy.
archive_warm_age = "30d"
archive_cold_age = "365d"

# Hard refuses to break audit chains; soft allows tombstoning sources
# whose semantic derivatives still exist (rare; usually a compliance op).
audit_chain_policy = "hard"   # hard | soft
```

The dream cycle (`spec-memory-maturation.md §6`) runs compaction at its scheduled cadence. TIDY does the actual work.

That's the full critical path.

If you want more (custom summarizers, cryptographic erasure, cold-tier federation, retention proofs for audit), use the navigator below.

---

## Choose Your Path

| You are building... | Read |
|---|---|
| **An extension that uses default compaction** | Quick Start above. |
| **A regulated extension** (per-regime retention + cryptographic erasure) | + §5 (Retention policy + erasure) + `spec-classification-boundary.md` |
| **A custom summarizer** (e.g., extension-specific compression heuristics) | + §2 (Summarize) + §3 (Custom summarizers) |
| **A multi-tier storage backend** | + §4 (Archive) + `spec-memory-maturation.md §9` |
| **A consumer that needs audit proof of retention** | + §6 (Tombstone semantics) + §7 (Audit trail) |

---

## 1) What this stage set owns

Compaction is three distinct operations grouped because they all *reduce* the ledger:

| Stage | Operation | What changes |
|---|---|---|
| **4. Summarize** | Lossy reduction | Replace `content.summary` (full original) with a shorter summary; mark `superseded_at` on the original; write a new `cognitive_type="episodic"` summary fragment with `compacted_from` provenance |
| **5. Archive** | Storage-tier move | Fragment's tier metadata transitions hot → warm → cold. Content moves to slower-tier backend. URI resolution stays transparent. |
| **6. Forget** | Tombstone (audit-preserved removal) | Append a tombstone record (audit log). Cryptographic erasure where keyed encryption-at-rest is used. Fragment removed from hot/warm tiers; cold tier may retain the ciphertext-without-key indefinitely depending on policy. |

Compaction **does not** produce semantic derivations (reflection's job). Compaction **does not** decide what was important (importance scoring's job at stage 2). It only reduces what's already there.

It **does** produce the tombstone-event channel that reflection consumes (§6).

---

## 2) Summarize (stage 4)

### 2.1 Trigger

A fragment becomes summarize-eligible when:

- Its `cognitive_type = "episodic"` (semantic / core are not summarized at this stage — see §8)
- It is older than `compaction.summarize_age` (per scope policy)
- It has at least one semantic fragment whose `derived_from` references it (i.e., consolidation has already extracted what was important)

The "consolidation-first" rule prevents losing detail before its insight has been captured. The dream cycle naturally orders stages this way; manual operator override can break the rule but is audit-logged.

### 2.2 Output

For each summarized source episode:

```python
composition.write(
    content={
        "summary": short_summary_text,
        "compacted_from": source_fragment_id,
        "original_length_chars": len(original_content),
        "compaction_version": "v1",   # the rule set used
        "tool": source.content.tool,
        "model": source.content.model,
        "fact_kind": "compacted_chat_turn",
        "event_time": source.content.event_time,
    },
    cognitive_type="episodic",
    principal_id=source.provenance.principal_id,
    agents={"axi-memory-compactor"} | source.provenance.agents,
    resources={f"axiom://memory/{source.fragment_id}"},
)
```

The source's `content.superseded_by = new_summary_id` is set; clients reading the source see "superseded — follow `superseded_by`."

### 2.3 Lossiness contract

Summarization is **lossy by design**. The original full content is no longer addressable via the hot-tier ledger after summarization completes — only the summary is. Three escape hatches:

- **Pre-tombstone retrieval** (§3): until the tombstone-age threshold fires, the original is still in warm tier and can be fetched via `axiom://memory/<id>?tier=warm`
- **Cold-archive retrieval** (§4.4): once archived, originals retrievable from cold tier per retention windows
- **Refuse-to-summarize override**: per-scope opt-out for audit-critical scopes (`compaction.summarize_disabled = true`)

---

## 3) Custom summarizers

The default summarizer is a deterministic length-reduction (drop assistant_output verbose details, keep user_input + summary). Extensions can register custom summarizers:

```toml
[[provides]]
kind = "compaction_summarizer"
ref = "my_extension.compact:my_summarizer"
scope_pattern = "my-extension-scope:*"
```

Contract:

```python
class CompactionSummarizer(Protocol):
    def summarize(self, source: MemoryFragment) -> CompactedContent:
        """Pure function: same source → same output. Must reduce length by ≥ 50%."""
```

LLM-driven summarizers are allowed but expensive; the dream cycle's per-scope budget (`spec-memory-maturation.md §6.4`) caps them. Deterministic summarizers are preferred for routine scopes.

---

## 4) Archive (stage 5)

Tier transitions:

| Transition | Trigger | What moves |
|---|---|---|
| Hot → Warm | `compaction.archive_warm_age` (default 30d) reached AND fragment not actively read in last `warm_cooldown` (default 7d) | Fragment content moved to warm-tier backend; tier metadata updated; URI resolution unchanged |
| Warm → Cold | `compaction.archive_cold_age` (default 365d) reached | Moved to cold-tier backend (object store, gzipped batches); URI resolution still transparent but latency target relaxes per `spec-memory-maturation.md §9` |
| Cold → Forget | Per retention policy (§5) | Tombstone path (§6) |

### 4.1 Backends

- **Hot**: `SQLiteBackend` (the default `ArtifactRegistry` backend)
- **Warm**: configurable per scope; defaults to a rotated SQLite shard at `~/.axi/memory/warm/`
- **Cold**: configurable; defaults to gzipped batches at `~/.axi/memory/cold/`; production options include SeaweedFS, S3-compatible object stores, encrypted blob

### 4.2 Air-gapped + edge

For air-gapped deployments, all tiers are local. For edge profile, warm and cold may both collapse onto a larger SQLite shard.

### 4.3 Federation + cohort cold

A cohort peer can serve as another node's cold tier under federation rules. Per `spec-federation-policy.md`, the gateway enforces classification routing: a CUI fragment cannot be cold-stored on a peer cohort whose trust profile lacks CUI capability.

### 4.4 Cold retrieval

Cold fragments resolve via `axiom://memory/<id>` transparently. Latency target relaxes to `< 5s p95`. Throughput throttled per `cold.max_concurrent_fetches` to avoid latency spikes. Repeated cold fetches of the same fragment promote it temporarily back to warm.

---

## 5) Forget (stage 6) — retention policy + cryptographic erasure

### 5.1 Retention windows

Per scope and per cognitive type:

```toml
[maturation."my-scope:*".retention]
episodic  = "180d"          # full forget after 180d
semantic  = "5y"
core      = "indefinite"
procedural = "indefinite"
resource  = "match_referent"  # follows the referent's retention
vault     = "policy_driven"   # uses spec-classification-boundary windows
```

Classification overrides scope retention monotonically: a CUI fragment has `cui_retention` (per `spec-classification-boundary.md`); the effective retention is `max(scope_retention, classification_retention)` unless the operator explicitly accepts shorter via legal-forget.

### 5.2 Per-regime retention

Per `spec-classification-boundary.md`:

| Regime | Default retention |
|---|---|
| `public` | scope-driven |
| `cui` | 7y from creation OR explicit forget event |
| `ear` | per-export-control-officer policy; typically 5y |
| `itar` | 5y minimum |
| `part_810` | per-DOE policy; commonly 25y |

WARDEN (Vega federation agent) audits retention enforcement on cohort-level reviews. Violation is a compliance defect, not a runtime error — the substrate logs and proceeds; remediation is operator-led.

### 5.3 Cryptographic erasure

Per ADR-026: where keyed encryption-at-rest is used, "forget" can be achieved by destroying the key for that scope-window even if the ciphertext remains. The tombstone records the key destruction with `forget_mode = "cryptographic_erasure"`. Three properties:

- **Reversibility**: forget is irreversible once the key is destroyed
- **Audit**: the key-destruction event is itself an immutable fragment (`fact_kind = "cryptographic_erasure"`)
- **Federation**: cohort peers holding ciphertext can no longer read it — the federation gateway treats key-destroyed fragments as unrecoverable

### 5.4 Legal-forget

User-initiated deletion requests (right-to-be-forgotten, GDPR-style):

```bash
axi memory forget --principal <p> --scope <s> --reason "user request 2026-05-12"
```

Bypasses the retention windows; subject to the operator's authorization. Audited via WARDEN.

---

## 6) Tombstone semantics + the event channel

A tombstone is itself a fragment:

```python
composition.write(
    content={
        "fact_kind": "tombstone",
        "target_fragment_id": <id>,
        "reason": "retention" | "operator_request" | "legal_forget" | "cryptographic_erasure",
        "forget_mode": "soft_delete" | "hard_delete" | "cryptographic_erasure",
        "audit_trail": {"actor": ..., "scope": ..., "ts": ...},
    },
    cognitive_type="episodic",
    principal_id="axiom-system",
    agents={"axi-memory-compactor"},
    resources={f"axiom://memory/{target_id}"},
)
```

### 6.1 Audit-chain enforcement

By default (`audit_chain_policy = "hard"`), a fragment cannot be tombstoned while any active semantic fragment cites it. The compactor refuses; SCAN logs the refusal so operators can act.

`audit_chain_policy = "soft"` allows tombstoning anyway; in this mode the tombstone event is published to the channel and downstream semantic fragments enter a "re-evaluation pending" state until next reflection cycle.

### 6.2 The tombstone-event channel

When a fragment is tombstoned, the substrate publishes an event:

```python
class TombstoneEvent(NamedTuple):
    target_fragment_id: str
    target_cognitive_type: str
    target_scope: str
    reason: str
    forget_mode: str
    timestamp: str
```

Consumers subscribe via `composition.subscribe_tombstones(scope_pattern)`. Reflection (`spec-memory-reflection.md §10`) is the primary consumer:

- **Hard policy**: reflection emits matching tombstone-via-compactor calls for every semantic fragment with `derived_from` referencing the target. Cascades through depth-2/3 derivations.
- **Soft policy**: reflection lowers `confidence` on dependent semantics; re-fires reflection at next cycle to either re-derive (if other sources support the insight) or tombstone (if no sources remain).

### 6.3 Idempotency

The tombstone event is published exactly once per target; consumers must be idempotent (a duplicate event is a substrate bug, not a normal occurrence).

---

## 7) Audit trail

Every compaction operation writes an audit fragment. The fragment is itself non-compactable (immutable audit). Schema:

```python
{
    "fact_kind": "compaction_event",
    "operation": "summarize" | "archive" | "forget",
    "target_fragment_id": <id>,
    "result_fragment_id": <id> | None,   # for summarize, the new summary id
    "scope": <scope>,
    "actor": "axi-memory-compactor" | "<operator-principal>",
    "policy_version": "v1",
    "tier_before": "hot" | "warm" | "cold",
    "tier_after": "hot" | "warm" | "cold" | "none",
    "reason": "scheduled" | "operator" | "legal_forget" | "cryptographic_erasure",
    "ts": ISO 8601,
}
```

WARDEN reads compaction-event fragments to verify retention compliance per cohort. Operators query via `axi memory audit --scope <s> --since <t>`.

---

## 8) Semantic and core fragments

Semantic and core fragments are typically *not* compacted at stages 4/5 in the same way episodes are. Why:

- Semantic fragments are already abstractions; further summarization tends to be lossy without proportional value
- Core fragments are identity/preferences; compacting them risks identity drift

That said:

- Semantic fragments **can** be summarized when a higher-order (weekly/monthly) semantic-of-semantic supersedes them. The supersession event triggers tombstone of the original semantic, not summarization.
- Core fragments support **supersession**: a newer `core` fragment with `content.supersedes = old_id` makes the older one a candidate for tombstoning at the next cycle.
- All three types are subject to **archival** (tier movement) on the same age-based policy.

Per-scope policy can override these defaults if a specific application needs compaction of semantics.

---

## 9) `axi memory` compaction verbs

```bash
axi memory compact --scope <s>                   # summarize-eligible sweep
axi memory compact --scope <s> --age 7d --dry-run

axi memory archive --scope <s> --to warm --age 30d
axi memory archive --scope <s> --to cold --age 365d
axi memory archive --scope <s> --fetch <fragment>  # promote cold → hot temporarily

axi memory retention --scope <s> --apply         # retention sweep (default dry-run)
axi memory retention --scope <s> --regime cui    # regime-specific sweep

axi memory forget --fragment <id>                 # operator-driven tombstone
axi memory forget --principal <p> --scope <s>    # legal-forget for a principal

axi memory audit --scope <s> --since <t>         # show compaction-event fragments
axi memory audit --retention-violations          # show fragments past retention
```

All verbs respect the policy gate and write audit fragments.

---

## 10) Per-extension compaction policies

Manifest declaration in Quick Start. Profile-driven defaults from `spec-memory-maturation.md §8`:

| Profile | `summarize_age` | `tombstone_age` | `archive_warm_age` | `archive_cold_age` |
|---|---|---|---|---|
| `default` | 7d | 30d | 30d | 365d |
| `aggressive` | 1d | 7d | 7d | 30d |
| `conservative` | 90d | never | 365d | never |
| `regulated` | 365d if retention allows | per-regime | 30d encrypted | per-regime; cryptographic erasure |

Scopes can override individual stages via the per-stage manifest (Quick Start example).

---

## 11) Cycle preemption + state preservation

If a dream cycle is interrupted mid-compaction (host restart, user activity burst):

- The current operation (one fragment's summarize / archive / forget) completes if started; the cycle aborts before the next operation
- The cycle's progress is recorded in a per-scope "cycle state" fragment so the next cycle resumes from where it stopped
- Tombstone events that were published before interrupt remain valid; consumers may receive them after a delay

---

## 12) Decided + open

**Decided (2026-05-12):**

- Compaction is three distinct operations (summarize / archive / forget) but one stage-set in the maturation lifecycle. They run in the same `axi memory compact` cycle, gated by per-fragment policy.
- Audit-chain enforcement (`hard` default): no tombstoning a fragment while active semantic derivatives cite it.
- Tombstone events are the single source of truth for downstream re-evaluation. Reflection consumes; compaction produces.
- Classification monotonicity (`spec-memory-maturation.md §7.3`) applies through compaction: a summary inherits the classification of its source.
- Cryptographic erasure where keyed encryption-at-rest is used. Per ADR-026.
- TIDY is the principal operator; WARDEN audits; SCAN monitors lag.

**Open (decide as impl proceeds):**

- **Cold-tier compaction recovery**: when a cold-archived episode is fetched for audit, do we re-promote it to warm permanently or just for the read? Cost-vs-fidelity tradeoff. Default position: temporary promotion, drop back to cold after `warm_cooldown`.
- **Cohort cold federation policy**: which peers can serve as cold for which scopes? Likely a per-scope `cold_backends = [peer1, peer2]` config; trust profile enforces classification routing.
- **Cryptographic-erasure replication**: when a federated cohort holds ciphertext, key destruction is local — the peer's ciphertext remains. Should we propagate "ignore this fragment from now on" to peers? Open: simplest is yes (peers honor a federation-broadcast tombstone); maybe too expensive to broadcast every key destruction.
- **Semantic supersession vs tombstone**: when a weekly-theme supersedes the dailies it derived from, do we tombstone the dailies or just supersede them? Default: supersede (keeps the dailies queryable for ~30d, then tombstones via retention).

---

## 13) Acceptance + tests

Phase-1 acceptance:
- Compaction sweep with default profile: episodes older than 7d with consolidation-derived semantics get summarized; original superseded; summary inherits classification + visibility
- Audit-chain enforcement: attempting to tombstone an episode whose semantic still cites it fails with a clear error
- Tombstone event published exactly once per target; reflection (in hard mode) tombstones derived semantics within one cycle
- Archive transitions: hot → warm at 30d default; warm → cold at 365d default; `axiom://` URI resolution transparent across tiers
- Retention sweep dry-run accurately reports what would be tombstoned per regime

Phase-2 (post-MVP):
- Cryptographic erasure: key destruction renders ciphertext unreadable on the local node; federated peers honor the broadcast tombstone
- Cohort cold-tier round-trip: write → archive to peer → fetch from peer → bytes match (after key resolution)
- Legal-forget: bypasses scope retention; audit trail is complete

---

## 14) Relationship to adjacent specs

- **`spec-memory-maturation.md`** — parent. Compaction is stages 4–6 within it.
- **`spec-memory-reflection.md`** — peer stage 3. Produces semantic fragments that compaction's audit-chain rule protects from premature tombstoning. Consumes tombstone events compaction publishes.
- **`spec-memory.md`** — substrate. Tombstone fragment shape + `cognitive_type` defined there.
- **`spec-classification-boundary.md`** — per-regime retention windows.
- **`spec-federation-policy.md`** — cohort cold-tier governance.
- **ADR-026 (ownership)** — cryptographic erasure mechanics.
- **ADR-027 (federated memory)** — cold-tier federation.
- **`prd-agents.md`** — TIDY ownership; WARDEN audit; SCAN monitoring.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
