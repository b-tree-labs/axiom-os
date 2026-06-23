# ADR-042: Chat-Driven Corrections + Correction-Aware Retrieval

**Status:** Proposed (2026-05-01)
**Authors:** Benjamin Booth, Claude
**Supersedes:** none
**Related:** ADR-026 (ownership model — corrections inherit ownership of the correction record, never the original); ADR-027 (federated memory — corrections propagate as new fragments through the existing addressing/propagation layer); ADR-028 (trust graph — cross-peer correction permission lookups); ADR-029 (federation composition — corrections are composed, not mutated); ADR-033 (layered memory architecture); ADR-035 (human-principal binding — every correction carries an accountable human); ADR-037 (federation state propagation — correction broadcast rides on the typed-record gossip primitive); ADR-041 (Identity Acquisition + Verification at Install Time — sister design, in flight; 041 establishes correct-the-first-time, 042 establishes correct-it-later); `spec-memory.md` (provenance contract); `spec-federation-policy.md` (visibility horizon for correction visibility).

---

## Context

A user discovered during a cross-vendor MCP demo that his Axiom node's identity contained a wrong owner email — a typo entered at install time. That email had been stamped into every memory fragment's `accountable_human_id`, every federation peer record, every signed compute receipt across roughly ten fragments and a half-dozen sessions. The provenance was correct in form (every write carried the accountable human per ADR-035) and wrong in fact (the human was named via a misspelled identifier).

The principle conflict surfaced sharply:

- **Immutable provenance** (ADR-027 + the `(T, U, A, R)` invariant in `spec-memory.md`): `(timestamp, user-principal, agents, resources)` is fixed at write time. Audit-grade. The architecture *must not* mutate fragments.
- **Reality**: humans correct facts in conversation constantly. *"Actually that's wrong, it's X."* The system today has nowhere for that correction to land except as a new free-form fragment that future readers will not connect to the original.

Today's surface is "manual correction-record fragment via direct compose call" — there is no first-class correction primitive, no MCP tool, no retrieval-time layering, no federation propagation. A user who notices the typo can type a new note ("my real email is X"), but every prior fragment continues to render the wrong value, and downstream consumers (RAG, cohort projections, audit reports) see no correction at all.

This ADR resolves the conflict by introducing **append-only correction-records + correction-aware retrieval**, modeled on the W3C PROV `prov:wasInvalidatedBy` + `prov:wasRevisionOf` patterns. Originals stay byte-identical and audit-grade; corrections are first-class signed fragments with a structured pointer to what they correct; the read path layers them at retrieval time.

ADR-041 (in parallel) reduces the rate of corrections by acquiring identity correctly the first time. ADR-042 acknowledges that no acquisition path is perfect, and gives Axiom a principled way to repair memory as a normal operation rather than a forensic exercise.

---

## Decision

### D1 — Corrections are NEW writes that REFERENCE the original; never modifications

A correction is a first-class `MemoryFragment` carrying a structured `CorrectionRecord` payload in its `content`. The original fragment stays byte-identical — its `id`, signature, provenance, and `(T, U, A, R)` tuple are untouched. The correction fragment has its own provenance (its own timestamp, its own `principal_id`, its own `accountable_human_id`, its own signature). Retrieval layers the correction on top of the original at read time.

This preserves ADR-027's federated-memory immutability (originals remain audit-grade) and ADR-035's human-principal binding (every correction has its own accountable human, distinct from the original's). It also preserves the `(T, U, A, R)` invariant *for both fragments simultaneously*: the original's tuple is what was true at original-write-time; the correction's tuple is what was true at correction-write-time. Both are audit-grade, and the relation between them is data, not metadata.

### D2 — Per-field correction granularity (dotted JSON path)

A correction targets a single field of a single original fragment, identified by a dotted JSON path: `owner`, `content.summary`, `content.value`, `provenance.accountable_human_id`. Multiple non-overlapping corrections can coexist against the same original; the read-path overlay applies them in `corrected_at` order.

We rejected per-fragment correction granularity (a correction supersedes the whole original). Per-field is more surgical, lets multiple correctors fix different fields without conflict, makes the diff visible to readers ("the email was wrong, but the summary was right"), and matches how PROV's `wasRevisionOf` is typically used in practice — at the entity-attribute level, not the entity level. Per-fragment correction is a degenerate case (correct every field at once); per-field is the general primitive.

Field paths target the *fragment's serialized shape* (i.e., paths into `to_dict()` output), not arbitrary computed views. Paths into `provenance.timestamp` and `provenance.principal_id` are **rejected** by the correction validator — those are the load-bearing audit-grade fields, and "I want to claim this fragment was actually written at a different time by a different actor" is forgery, not correction. Paths into `provenance.accountable_human_id` are **accepted** because that is exactly the demo's case and the pattern we expect to see most often in practice.

### D3 — Correction records are MemoryFragments with `cognitive_type="correction"`

We add a seventh value to `CognitiveType` (the existing six are core / episodic / semantic / procedural / resource / vault per ADR-033 and MIRIX). The correction type carries a content-shape validator:

```python
@dataclass(frozen=True)
class CorrectionRecord:
    corrects_fragment_id: str            # the original
    corrects_field: str                  # dotted JSON path into original.to_dict()
    wrong_value: Any                     # what was there at correction time
    correct_value: Any                   # what should be
    reason: str                          # human-readable why
    corrector_principal: str             # who is making this correction
    corrected_at: str                    # ISO timestamp
    chat_turn_ref: str | None = None     # optional: link to the chat turn
    supersedes_correction: str | None = None  # for chains
```

The correction fragment's `provenance` is normal — it is the provenance of the correction itself (when corrected, by whom, under what accountable human, with what agents). The correction's signature signs the `CorrectionRecord` payload + the standard provenance, exactly as a normal fragment signature does.

We chose a new `cognitive_type` (rather than overloading `episodic`) because:
- Type-specific validators (`_validate_content` in `fragment.py`) cleanly enforce the `CorrectionRecord` shape at construction time.
- Storage layers can index corrections separately for the read-path overlay query.
- Audit projections (`axi memory show`, federation gateway) can filter corrections distinctly without inspecting `content`.
- Per-type retention policy can differ: corrections never expire (they are part of the audit chain forever), even when their original may be archived.

### D4 — Default retrieve behavior is corrections-applied; raw is opt-in

`axiom_memory__retrieve` (and the underlying `CompositionService` retrieval API) returns corrected values *by default*. The response shape adds a `corrections: list[CorrectionRecord]` field per fragment so consumers can see the chain that produced the corrected view.

```jsonc
{
  "fragment_id": "frag-abc",
  "content": { "owner": "user@example.org" },           // corrected
  "raw_content": { "owner": "user.old@example.org" },   // original
  "corrections": [ { "corrects_field": "owner", ... } ]
}
```

`include_corrections=False` returns the raw historical view (no overlay applied, no `corrections` key). `axi memory show --raw` is the CLI pass-through.

We rejected raw-by-default because the overwhelming majority of callers (chat agents pulling context, RAG building prompts, cohort projections, federation peers reading shared memory) want truth, not history. Forensic readers (audit, retraction review, schema migration) are the minority and explicitly opt in.

This is the load-bearing UX call: the system *prefers* corrected values to historical values for any consumer that does not declare a forensic intent. Anything else makes corrections invisible by default, which makes them useless.

### D5 — Correction-of-correction chains are allowed; superseded corrections are skipped on overlay

A correction `C2` may set `supersedes_correction=C1` to indicate that `C2` replaces `C1` (e.g., the first correction was itself wrong, or a later corrector has better information). The retrieval overlay walks the chain head-to-tail, applies the freshest non-superseded correction per field, and surfaces the full chain in the `corrections` array so the reader sees the supersede graph.

Chains are ordered by `corrected_at`. Cycles are rejected at write time (`C1.supersedes = C2 AND C2.supersedes = C1` is a write-time error). The overlay prefers the chain head (most recent, non-superseded); `axi memory correction-log <fragment_id>` exposes the full DAG.

We allow chains because the *real* failure mode is "the user typed the wrong correct_value" — and the only graceful repair is a correction-of-correction. Forbidding chains would force users to either accept incorrect corrections or to mutate them, the latter of which violates D1.

### D6 — Cross-peer correction trust defaults to permissive within cohort, restrictive across cohorts

Within a single cohort, *any cohort member can correct any fragment they can see*. Cohort membership already implies a meaningful trust floor (per ADR-028 trust graph admission_threshold and ADR-027 cohort registry), and corrections are themselves auditable signed fragments — the corrector cannot hide who they are.

Across cohorts (a peer in cohort A wants to correct a fragment owned by cohort B), corrections require a `(corrector, original_owner, "memory.correct")` trust-graph edge per ADR-028. The default cross-cohort trust score for `memory.correct` is **below** the admission threshold — meaning cross-cohort corrections require an explicit trust assertion, not just visibility.

We chose permissive-within-cohort because the cohort-mate scenario *is* the normal case (the instructor corrects a student's stale fact in NE-101; a TA fixes a typo in a course note) and friction here defeats the purpose. We chose restrictive-across-cohort because cross-cohort correction is asymmetric — peer A may not know what peer B's policy is for accepting corrections, and the trust graph is the right place to express that.

A cohort root may override either default: a classified-cohort root may set "only the original author may correct"; a research-program root may set "any peer in the federation may correct, low-trust corrections are flagged for review."

### D7 — Two propagation patterns: local-overlay (default) and broadcast (opt-in)

Two patterns are supported, both rest on existing federation primitives:

**Pattern 1 — Local correction with remote-visibility opt-in.** I write a correction in my own store; the correction is a normal fragment with my visibility horizon (per `spec-federation-policy.md`); if I share my store with the cohort (which is what cohort membership implies for memory fragments), peers see my correction through the standard ADR-027 propagation path. This is permissionless — it requires only that I can write to my own store.

**Pattern 2 — Correction broadcast to original's home node.** I push the correction record to the original's home node via the ADR-027 federation propagation, with intent flag `correction_for_remote_origin=true`. The receiving node accepts the correction (subject to D6 trust check) and appends it to *its* correction store, where it becomes visible to all of that node's other clients. This requires the origin node to trust me to write into its correction layer.

Pattern 1 is the default for chat-driven corrections (the user fixes their *own* memory; the correction propagates because the user shares with the cohort). Pattern 2 is opt-in via an explicit `--broadcast` flag on `axi memory correct`, and surfaces a one-line confirmation when crossing into untrusted territory ("This correction will be sent to peer X. Continue?").

Per ADR-037, corrections are also a candidate record type for the federation directory's typed-record gossip — high-impact corrections (e.g., to identity fields) propagate quickly via the federation directory's existing gossip cadence rather than waiting for a memory pull. Implementation detail deferred to the spec.

### D8 — Display chrome makes corrections visible without hiding originals

Every read surface that renders a fragment renders the correction state. The canonical display vocabulary:

- **CLI long form (`axi memory show`):** original value rendered with strikethrough; corrected value inline next to it; correction byline (who, when, why) on the next line; chain depth indicated if `supersedes_correction` is non-null. Example:
  - `owner: user.old@example.org → user@example.org  (corrected by @laptop:user at 2026-05-02; reason: "typo at install time")`
- **CLI compact (`axi memory list`):** correction count badge — `[c2]` for "two corrections applied".
- **CLI raw (`axi memory show --raw`):** no overlay; original-only; explicit `[2 corrections suppressed]` banner so the reader is not misled about what they are seeing.
- **MCP retrieve response:** structured `corrections` array per D4; consumers render however suits their surface.
- **Chat surface:** corrections rendered as a sub-line under the cited fact ("source: frag-abc, corrected 2026-05-02 by @laptop:ben"). Chat agents must surface corrections when citing — silently using a corrected value without disclosing the correction is forbidden by the chat-skill contract.

The vocabulary is canonical. Every surface in Axiom uses these terms. Extensions extending the display surface inherit the contract.

### D9 — Chat-driven correction is the killer demo path

When a user types in chat *"actually my email is user@example.org, not user.old@example.org"*, the agent (typically WALL-E) executes:

1. **Disambiguate.** Find candidate fragments via semantic search OR explicit `corrects: <id>` reference in the user's message. Multi-match is surfaced for confirmation: *"I see this email in 11 fragments — should I correct all of them, or only the most recent?"*
2. **Compose correction-records.** One per affected fragment, signed by the calling principal under the user's `accountable_human_id`. Per-field path; the agent infers the path from the disambiguation step.
3. **Echo back.** A receipt-style summary ("I corrected `owner` in 11 fragments, ids: …, at 2026-05-01T22:42Z, under accountable human @user:example-org. Here is the audit log link.").
4. **Future retrievals automatically apply the corrections.** The same RAG/context build the agent will run on the next turn now sees the corrected values.

This is the **groundbreaking use of MCP** the demo audience saw missing in cross-vendor harnesses: the agent is not just remembering — it is actively repairing its own memory in response to user feedback, in a way that preserves the audit trail. No competitor harness has the immutability invariant *and* the live-repair surface, because they have neither.

### D10 — Retraction (per ADR-035 §D4) and correction are different operations

A correction says *the value was wrong, here is the right value, both records are part of the audit trail.* A retraction says *I no longer authorize forward derivation from this fragment, but the historical record persists for audit.* They are distinct primitives with distinct surfaces. Retraction is anchored on the accountable human (per ADR-035 §D4); correction is anchored on a per-field claim with no authority requirement beyond D6.

A correction does *not* retract the original. A retraction does *not* correct anything — it stops forward derivation but does not change the fragment's content. Both can coexist on the same original (a retracted-but-corrected fragment is a meaningful state: "do not derive from this further, and also it had a wrong field value").

### D11 — The `axiom_memory__correct` MCP tool is the canonical surface

All correction operations go through one MCP tool: `axiom_memory__correct(fragment_id, field, correct_value, reason, *, broadcast=False, supersedes_correction=None)`. The tool returns the new correction-fragment id + a summary. The CLI (`axi memory correct`) and the chat-driven path (D9) both call the same tool.

`axiom_memory__retrieve` returns corrections by default (D4). `axiom_memory__correction_log(fragment_id)` returns the chronological correction chain. These three tools are the entire correction surface; no other MCP entry points are introduced.

### D12 — Correction records are first-class for trust adaptation

A pattern where a corrector consistently produces accepted-and-not-superseded corrections is positive trust signal per ADR-028's adaptation loop. A pattern where a corrector's corrections are repeatedly superseded (their corrections were themselves wrong) is negative signal. The correction store feeds the trust adaptation hook — corrections become an observation source for the trust graph, the same way successful tool calls and failed calls are today.

This closes the loop: the system that lets you fix its mistakes also learns who is good at fixing things.

---

## Rationale

### Why append-only beats mutate-with-history

We considered two alternatives:

1. **Mutate the fragment, write a separate audit log of the change.** Rejected because it violates ADR-027's federated-memory immutability (signatures over the post-mutation fragment break verification of the original; audit becomes a separate-store concern subject to drift). It also weakens the federation propagation story: peers who already replicated the original now have a different fragment from the home node, and reconciliation is undefined.
2. **Version the fragment (v1, v2, v3 of the same id).** Rejected because it confuses identity with content. ADR-027's `axiom://node/fragment-id` is meant to address a specific immutable artifact. Versioning would force every consumer to think about which version they want, every signature to be scoped to a version, every cohort registry entry to track versions. The complexity dwarfs the benefit.

Append-only correction-records preserve all the existing invariants (immutability, signatures, addressing, propagation) and add the correction layer purely on top. Originals never change; consumers who do not care about corrections see the same fragment they always did; consumers who care get the overlay for free.

### Why W3C PROV is the right precedent

PROV is the standard for provenance metadata across audit-grade systems (W3C Recommendation since 2013). Its `wasInvalidatedBy` (the original is no longer current) and `wasRevisionOf` (this entity is a revision of that entity) patterns are exactly what corrections need: append-only, structured pointers, no mutation. Aligning with PROV gives Axiom a defensible standards story for any future regulator or auditor question, and a documented precedent for the field-granularity choice.

### Why per-field beats per-fragment

A real fragment carries multiple facts. The owner field can be wrong while the summary field is right. Per-fragment correction would force the corrector to copy the still-right facts into the correction (creating divergence risk if the original is later updated through other means) or to leave them implicit (creating ambiguity about what the correction actually claims). Per-field corrections are atomic statements: "this one field, this one value, here's the right value." They compose cleanly.

The cost is overlay complexity at read time — the retrieval path must walk the correction set and apply per-field. This is bounded (corrections per fragment are small in practice; field paths are simple JSON pointers) and indexable.

### Why corrections-applied-by-default

Anything else makes corrections invisible by default, which defeats the point. The mental model is: *the system stores history, but presents truth.* Forensic consumers explicitly say "show me history"; everyone else gets truth without having to ask.

### Why permissive-within-cohort and restrictive-across-cohort

Cohorts already encode a meaningful trust boundary (ADR-027 cohort registry, ADR-028 trust admission). Inside a cohort, friction in correcting peers' work damages the cohort's collective intelligence — instructors cannot fix student stale facts; TAs cannot patch course-note typos; researchers cannot annotate each other's findings. Outside a cohort, the asymmetric-knowledge problem dominates — peer A may not know peer B's correction policy, and the trust graph is the right vocabulary for "I trust this peer to correct my fragments."

The cohort root override (D6) is the escape hatch for cohorts whose policy differs from the default (classified, regulated, or single-author cohorts).

---

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Mutate fragments + separate audit log | Breaks ADR-027 federated-memory immutability and signature semantics. |
| Version fragments (v1, v2, v3 of same id) | Confuses identity with content; every consumer must reason about versions. |
| Per-fragment correction granularity | Forces re-stating still-true facts; ambiguous when partial. |
| Raw-by-default retrieval | Makes corrections invisible by default; defeats the purpose. |
| Forbid correction-of-correction chains | Forces users to mutate or accept wrong corrections; violates D1. |
| Restrictive-within-cohort (only original author may correct) | Damages cohort collective intelligence; instructor cannot fix student fact. |
| Permissive-across-cohort by default | Asymmetric-knowledge problem; cross-cohort policy varies. |
| Single broadcast pattern (always push to remote) | Trust-gated; overkill for the common case where local-overlay suffices. |
| Single overlay pattern (no broadcast) | Cannot fix peer's home-node view; peer's other clients still see the wrong value. |
| Correction = retraction | Different semantics; ADR-035 retraction is forward-derivation control, not value claim. |

---

## Consequences

### Positive

- **Preserves ADR-027 immutability.** Originals are byte-identical; signatures still verify; federation propagation unchanged.
- **Preserves ADR-035 accountability.** Every correction has its own accountable human, distinct from the original's. The chain is auditable.
- **First-class chat-driven repair surface.** D9 is a competitive differentiator — Codex / Claude Code / Cursor / Devin have neither the immutability invariant nor the federation surface to deliver this.
- **Audit-grade correction trail.** PROV-aligned; defensible to regulators; first-class in the audit projection.
- **Composable.** Per-field, chainable, federation-aware. The primitive composes; the surface composes; the trust signal composes.
- **Closes the loop on the install-time identity bug.** The demo failure that surfaced this work has a graceful repair path; the system that made the mistake is now the system that fixes it.

### Negative / costs

- **New `cognitive_type`.** Memory-persistence-plan implications: schema_version bump (correction is a new content shape), per-type validator, retention policy entry, fixture variant. Per the existing schema-version policy, mechanical but real.
- **CompositionService write path additions.** A `correct(...)` entry point in addition to `write(...)`, with the CorrectionRecord validator, the supersedes-cycle check, and the trust-edge check (D6).
- **Retrieval path overlay.** Every retrieve, every projection, every audit query must walk the correction set and apply the overlay. Indexable, bounded, but ubiquitous.
- **Display surface expansion.** Every CLI command, every chat surface, every MCP response that renders fragments must render corrections. Mechanical sweep but real.
- **Federation gateway changes.** Correction propagation modes (D7) require gateway support for the `correction_for_remote_origin` intent flag and the cross-cohort trust check.
- **Trust-graph integration.** D12's adaptation loop signal is a new observation type for the trust graph.

### Risks + mitigations

| Risk | Mitigation |
|---|---|
| Correction overlay performance on hot retrieval paths | Index corrections by `corrects_fragment_id`; cache per-fragment overlay result; benchmark in retrieval gate. |
| User overcorrects (corrects values that were right) | The original is preserved; a correction-of-correction with `supersedes_correction` is the graceful fix; trust signal D12 catches the pattern. |
| Cross-cohort correction floods (peer corrects everything they see) | D6 trust gate catches this; trust adaptation downgrades the offender; cohort root can revoke `memory.correct` rights. |
| Retrieval consumer assumes raw without opting in | Corrections-by-default is documented; the response always carries `corrections` so the consumer knows; raw mode is explicit. |
| Correction chains become deep / unreadable | `axi memory correction-log` is the canonical surface for chain inspection; display compact form shows badge `[c2]`; long form shows last hop only with chain expansion on flag. |
| Bad-faith correction (correcter claims a wrong value as the correction) | Signature + accountable-human binding makes the corrector identifiable; trust signal D12 catches systematic offenders; cohort root can revoke. |
| Pattern 2 broadcast accepted by misconfigured peer | Receiving node enforces D6 trust check; trust graph default fails closed across cohort; explicit `--broadcast` confirmation in CLI. |

---

## Compliance gates introduced

A new `pytest -m correction_compliance` marker covers:

- Correction record write produces a new fragment; original is byte-identical pre/post.
- Correction signature verifies independently of the original.
- Retrieval with default `include_corrections=True` returns corrected values + the corrections array; `include_corrections=False` returns originals.
- Per-field paths into `provenance.timestamp` and `provenance.principal_id` are rejected.
- Correction-of-correction chains apply the freshest non-superseded correction per field; cycles are rejected.
- Within-cohort correction succeeds without explicit trust edge; cross-cohort correction requires explicit edge.
- Pattern 1 propagation rides on existing memory propagation; Pattern 2 broadcast invokes the gateway intent flag.
- Display chrome renders the canonical vocabulary (D8) on every surface that renders fragments.
- Trust adaptation receives correction observations (D12).

This gate joins `accountability_compliance` (ADR-035) and `memory_compliance` as a release gate.

---

## Phasing

Prague go-live is roughly five weeks out (project memory: `project_prague_go_live_date.md`). This ADR is **post-Prague** for full delivery.

- **Now (today's surface, pre-ADR):** Manual correction-record fragment via direct compose call. No first-class primitive, no MCP tool, no overlay, no propagation. State: known limitation; documented in `working/`.
- **Phase 0 (post-Prague Week 1–2):** ADR ratification + `CognitiveType.CORRECTION` + `CorrectionRecord` schema + per-type validator + CompositionService `correct(...)` + retrieval overlay (default-on) + `axi memory correct` / `--raw` / `correction-log` CLI. Single-node only. No federation propagation.
- **Phase 1 (post-Prague Week 3–4):** `axiom_memory__correct` + `axiom_memory__correction_log` MCP tools. Chat-driven correction agent flow (D9) in WALL-E. Display chrome (D8) across CLI + chat. `correction_compliance` gate.
- **Phase 2 (post-Prague Week 5–6):** Cross-peer correction with D6 trust check; Pattern 1 propagation via existing ADR-027 path; cohort-root policy override for D6 default.
- **Phase 3 (post-Prague Week 7+):** Pattern 2 broadcast via federation gateway intent flag; D12 trust adaptation integration; correction record type in ADR-037 federation directory for high-impact identity-field corrections.

For Prague itself, the workaround is documented and operational: students or instructor who notice a bad fact in their memory open a chat with WALL-E, who writes a normal episodic fragment ("the previous fact about X is wrong; the correct value is Y") and tags it for the post-Prague migration helper. The post-Prague Phase 0 ships a `axi memory migrate --convert-correction-notes` helper that walks these tagged fragments and converts them to first-class corrections.

This is honest about Prague: corrections are *not* a Prague feature; the workaround is a normal episodic fragment; the migration is well-defined.

---

## Open items

- **Correction record retention.** D3 says corrections never expire. Edge case: the original is retracted (ADR-035 §D4) and forward-derivation is stopped. Does the correction also stop being applied? Proposed: yes — a retracted original suppresses correction overlay (the retraction is the user saying "do not derive from this," and applying the correction *is* a kind of derivation). Confirm in spec.
- **Correction visibility horizon.** D7 Pattern 1 says corrections inherit the corrector's visibility horizon. Edge case: I correct a SCOPE_PUBLIC fragment with a SCOPE_INTERNAL correction. Does the correction propagate with the original? Proposed: visibility is `min(original.visibility, correction.visibility)` for the overlay; raw correction record honors its own visibility. Confirm in spec.
- **Schema migration concurrency with ADR-035.** ADR-035 introduced `schema_version=2`; this ADR may bump to `schema_version=3`. Coordinate with `working/memory-persistence-plan.md` to ensure Phase 0 lands cleanly atop the v2 baseline.
- **Render chrome on third-party MCP clients.** Display chrome (D8) is a contract for Axiom-shipped surfaces. Third-party MCP clients (Claude Desktop, Cursor, etc.) consuming `axiom_memory__retrieve` get the structured `corrections` array but cannot be forced to render it. Mitigation: tool description + system-prompt guidance; long-term, dual-track standards push (ADR-032) to land "corrections" as a first-class field in the AAIF retrieval response shape.
- **Correction-of-retraction.** Can you correct a retraction? (User says "I never retracted this; that retraction was someone else acting under my account.") Proposed: yes — retractions are themselves fragments, so they are correctable. Edge case worth a separate test.

---

## The bottom line

The conflict was real: provenance immutability versus the human reality of needing to fix things. The resolution is the W3C PROV pattern adapted to Axiom's federated, signed, accountable-human-bound memory: corrections are new writes that reference originals, retrieval layers them by default, federation propagates them through the existing primitives, the trust graph learns from who corrects well. Originals never change. The truth shifts on top.

The demo audience saw a system that knew it had been misnamed and could not say so. The next demo is a system that, when told it was misnamed, names the correct one — in the chat, in the fragment, in the audit log, in every peer that asked — without forging a single bit of history. That is the killer use of MCP that no peer harness can match without rebuilding their session model from the ground up.

ADR-041 will reduce the rate of corrections by acquiring identity correctly the first time. ADR-042 acknowledges that no acquisition is perfect, and gives Axiom a principled way to repair memory as a normal operation. The two are complementary; both are load-bearing.

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
