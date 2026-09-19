# Product Requirements — Chat-Driven Corrections + Correction-Aware Retrieval

**Product / Feature:** Chat-Driven Corrections (`axiom_memory__correct` + correction-aware retrieval)

**Owner:** Axiom Memory team   •   **Status:** Draft   •   **Last updated:** 2026-05-01

**Related:** ADR-042 (architecture), `spec-chat-driven-corrections.md` (technical spec), ADR-027 / ADR-035 (load-bearing dependencies), ADR-041 (sister design — Identity Acquisition + Verification at Install Time, in flight).

---

## 1) Elevator Pitch

Axiom is the only agent platform where, when you tell the assistant *"that's wrong, the right value is X,"* the assistant repairs its own memory in front of you — preserving the audit trail of what was wrong, why it was wrong, and who fixed it — and from that moment forward, every retrieval, every cohort peer, and every downstream agent sees the truth.

## 2) Problem / Opportunity

- **The conflict:** Audit-grade memory must not mutate (provenance immutability per ADR-027). Human reality: facts go stale, get typo'd, or come in wrong from install-time identity capture. Today users have nowhere for "actually that's wrong" to land except a free-form note that future readers will not connect to the original.
- **The trigger:** A cross-vendor MCP demo surfaced an install-time email typo that had stamped the wrong identifier into ~10 fragments and a half-dozen sessions. The user could see the bug; the system had no first-class way to fix it.
- **Who's impacted:**
  - **Every Axiom user**, the moment they type a wrong value into a fact the agent records.
  - **Every cohort participant** whose peers cite stale or wrong facts back to them ("the instructor cited my old email").
  - **Every regulator / auditor / classification reviewer** who needs a defensible "we know this fact was wrong, here is the correction record" answer.
- **Why it matters now:** The architecture forces this design *now*, not later. Adding it later means migrating already-stale fragments under cohort scrutiny. Adding it now (post-Prague) means it ships with the federation propagation layer already known about and accounted for.
- **Competitive context:** No peer harness (Codex, Claude Code, Cursor, Devin) has both the immutability invariant *and* the live-repair surface — they have neither. This becomes a load-bearing differentiator.

## 3) Goals & Success Metrics

- **Primary goal:** Make memory correction a normal, conversational operation — type the correction, see it applied, see the audit trail, see future retrievals reflect it.

| # | Metric | Target |
|---|---|---|
| 1 | Time from "user types correction in chat" → "next retrieval returns corrected value" | ≤ 5 seconds (single-node); ≤ 30 seconds (cross-cohort propagation Pattern 1) |
| 2 | Correction round-trip preserves byte-identity of original fragment | 100% (compliance gate) |
| 3 | Default retrieve returns corrected values + corrections array | 100% of `axiom_memory__retrieve` calls without `include_corrections=False` |
| 4 | Per-field correction granularity supported | All non-provenance-load-bearing fields (`provenance.timestamp` and `provenance.principal_id` rejected by validator) |
| 5 | Correction chain depth handled correctly | ≥ 5-deep `supersedes` chains traversed in retrieval overlay; cycles rejected at write |
| 6 | Within-cohort correction succeeds without explicit trust edge | 100% of within-cohort `axi memory correct` calls |
| 7 | Cross-cohort correction requires explicit `(corrector, owner, "memory.correct")` trust edge | 100% (rejection + clear error if missing) |
| 8 | Display chrome canonical vocabulary on every fragment-rendering surface | CLI long, CLI compact, CLI raw, MCP retrieve response, chat citation — all five surfaces |
| 9 | `pytest -m correction_compliance` passing | Green at every release tag from Phase 1 onward |
| 10 | Chat-driven correction agent flow works end-to-end with disambiguation | "fix my email" / "fix my email everywhere" / "fix my email only in the latest session" all resolve correctly |

## 4) Key Users / Personas

- **Persona A — End user (non-technical / semi-technical).** Notices a wrong fact in the assistant's memory or in a citation. Wants to type the correction and have it stick. Does not want to learn a CLI. Should never have to invoke a migration script. *Primary audience for D9 chat flow.*
- **Persona B — Cohort instructor / TA.** Notices a stale fact in a student's note or the cohort knowledge base. Wants to correct it for the cohort, with byline. Comfortable with chat or CLI. *Primary audience for within-cohort permissive correction.*
- **Persona C — Operator / auditor.** Reviewing the audit projection. Wants to see the full correction chain, who corrected what, when, why; wants to verify the original is byte-identical and the chain is signed. Comfortable with `axi memory correction-log` and `--raw`. *Primary audience for forensic surface.*
- **Persona D — Federation peer.** A cohort-mate or cross-cohort peer who learns a fact in their store is stale. Wants to either correct it locally with cohort visibility (Pattern 1) or push the correction to the original's home (Pattern 2). Comfortable with the trust-graph implications. *Primary audience for D7 propagation patterns.*

## 5) Scope — Key Capabilities (MVP)

1. **`axiom_memory__correct(fragment_id, field, correct_value, reason)` MCP tool** — writes a signed correction-record fragment; returns new fragment_id + summary; rejects loaded-bearing-provenance paths.
2. **Correction-aware `axiom_memory__retrieve`** — returns corrected values by default; includes `corrections` array per fragment; `include_corrections=False` returns raw originals.
3. **`axiom_memory__correction_log(fragment_id)` MCP tool** — chronological correction chain for a fragment, including `supersedes` graph.
4. **`axi memory correct` / `axi memory show --raw` / `axi memory correction-log` CLI** — human-facing surfaces; canonical display chrome.
5. **Chat-driven correction agent flow (WALL-E)** — disambiguate, compose corrections, echo receipt, future retrievals reflect corrections.
6. **Per-field correction granularity** — dotted JSON path; multiple non-overlapping corrections coexist; provenance.timestamp/principal_id rejected.
7. **Correction-of-correction chains** — `supersedes_correction` field; cycles rejected; overlay applies freshest non-superseded.
8. **Within-cohort permissive / cross-cohort restrictive trust** — D6 default; cohort-root override.
9. **Pattern 1 propagation (local-overlay with cohort visibility)** — rides on existing ADR-027 propagation.
10. **`pytest -m correction_compliance` gate** — release-blocking.

## 6) Non-Functional / Constraints

- **Performance:** Retrieval overlay adds ≤ 10ms per fragment for ≤ 10 corrections; ≤ 50ms for ≤ 100 corrections (benchmarked in retrieval gate).
- **Storage:** Correction records are normal MemoryFragments — no new storage path; reuse the existing artifact registry.
- **Backward compat:** Pre-correction fragments retrieve identically (no overlay applied if no corrections exist). No migration of existing fragments required for read.
- **Schema:** New `cognitive_type="correction"`. Schema_version bump coordinated with ongoing memory-persistence-plan (will be v3 atop ADR-035's v2).
- **Federation:** Pattern 1 uses existing ADR-027 propagation; no new transport. Pattern 2 (deferred to Phase 3) adds a gateway intent flag.
- **Display chrome:** Canonical vocabulary defined in spec §6; every Axiom surface that renders fragments must conform; AEOS conformance gate per ADR-031 / spec-aeos.
- **Domain-agnostic:** No nuclear / reactor / institution names in core docs or test fixtures. Personas in this PRD are role descriptions.

## 7) Timeline (high level)

Prague go-live is roughly five weeks out (early June 2026). Corrections are **post-Prague**. Pre-Prague workaround: free-form episodic note tagged for migration helper.

- **Pre-Phase 0 (now → Prague):** Document the workaround pattern; ship migration-helper stub that recognizes tagged notes for later conversion.
- **Phase 0 (post-Prague Week 1–2):** ADR ratification + new cognitive type + CompositionService correct path + retrieval overlay (default-on) + CLI. Single-node only.
- **Phase 1 (post-Prague Week 3–4):** MCP tools + chat-driven flow (WALL-E) + display chrome on all surfaces + `correction_compliance` gate live.
- **Phase 2 (post-Prague Week 5–6):** Cross-peer correction with trust check + Pattern 1 propagation + cohort-root D6 override.
- **Phase 3 (post-Prague Week 7+):** Pattern 2 broadcast via gateway + trust adaptation feedback (D12) + federation directory record type for high-impact corrections.

## 8) Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Correction overlay performance regresses retrieval-hot paths | Benchmark in retrieval gate; index by `corrects_fragment_id`; cache per-fragment overlay. |
| Users overcorrect (correct values that were right) | Original is preserved; correction-of-correction is graceful repair; trust signal D12 catches systematic offenders. |
| Cross-cohort correction floods | D6 trust gate; explicit `--broadcast` confirmation; cohort root can revoke `memory.correct` rights. |
| Third-party MCP clients ignore the `corrections` array | Tool-description guidance + system-prompt examples; long-term AAIF push to land "corrections" as standard field. |
| Schema-version coordination with ADR-035 | Land 042's bump to v3 only after v2 (ADR-035) is stable; per memory-persistence-plan §3 cadence. |

**Open questions** (decide before Phase 0 ratification):

- Does a retraction (ADR-035 §D4) on the original suppress the correction overlay? (ADR proposes yes; spec confirms.)
- Visibility-horizon arithmetic for corrected fragments: `min(original.visibility, correction.visibility)` for overlay, or correction-honors-its-own? (ADR proposes min; spec confirms.)
- Render chrome contract for third-party MCP — tool description + system prompt sufficient, or AEOS extension surface needed? (Spec scoping.)

## 9) Acceptance & Rollout

**Sign-off:** Memory team lead (architectural), Federation team lead (D7 propagation), Trust team lead (D6 + D12), Chat team lead (D9 WALL-E flow). PRD review with project owner before Phase 0 starts.

**Rollout:** Phased per §7. Each phase is independently shippable and end-to-end useful (per `feedback_phased_work_must_deliver_per_phase` — no plumbing-now-feature-later).

- Phase 0 ships: single-node correction surface usable today by any Axiom user via CLI.
- Phase 1 ships: chat-driven correction usable end-to-end with WALL-E; the killer demo recorded.
- Phase 2 ships: cohort-mate correction; instructor-corrects-student-note works.
- Phase 3 ships: cross-cohort correction with broadcast; full federation propagation.

**Rollback criteria:** A correction overlay bug that returns wrong values (worse than no correction at all) reverts to `include_corrections=False` default with banner; full rollback if signature verification of the original is breakable post-correction (which the architecture forbids — would indicate a serious implementation defect).

## 10) Contacts & Links

- Product lead: Benjamin Booth, no-reply@axiom-os.ai
- ADR: [adr-042-chat-driven-corrections-and-correction-aware-retrieval.md](../adrs/adr-042-chat-driven-corrections-and-correction-aware-retrieval.md)
- Spec: [spec-chat-driven-corrections.md](../specs/spec-chat-driven-corrections.md)
- Related ADRs: ADR-027 (federated memory), ADR-035 (human-principal binding), ADR-028 (trust graph), ADR-037 (federation state propagation), ADR-041 (identity acquisition — sister design)

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
