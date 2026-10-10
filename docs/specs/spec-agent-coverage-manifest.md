# Agent Coverage Manifest — Specification

**Status:** Active — Reference Implementation
**Version:** 0.1.0
**Editor:** Benjamin Booth
**Date:** 2026-05-03
**Reference implementation:** Axiom platform (`b-tree-labs/axiom-os`)

---

## Abstract

The Agent Coverage Manifest is a versioned registry mapping **observable platform conditions** to **owning agents**, **detection methods**, and **response RACI**. It exists because agent personas describe what each agent owns, but personas alone do not enumerate what conditions the fleet collectively MUST observe. New failure classes that don't fit any existing persona's mental model fall through gaps silently, surfacing only when a human notices. The manifest closes that gap by making coverage an explicit, auditable artifact.

The manifest is a sibling concern to the AEOS specification: AEOS governs *what extensions are*; the Coverage Manifest governs *what the running fleet watches*. Both are required for a federation-grade agent platform.

---

## 1. Motivation

### 1.1 The originating incident (2026-05-03)

A pre-push hook surfaced ~41 sustained `pytest` failures on `main`. The failures had been present for days. None of the deployed agents detected them; the platform learned about the regression only when a human ran a release sweep. Five Whys traced the root cause to a structural gap:

- **TIDY** owns hygiene and runs a CI watcher, but `ci_watcher.py` queries *remote* CI provider runs only — it does not sample local `pytest` health.
- **TRIAGE** owns diagnostics, but its heartbeat scans signatures, TOFU events, and security-relevant configuration — not test-suite health.
- **RIVET** owns release lifecycle, but its gating only triggers when a human invokes a release.
- **PRESS** owns publishing, **CURIO** owns research, **AXI** owns chat — none claim test-suite hygiene.

Each agent's persona was internally consistent. The gap was between personas, not within any one persona. **Personas describe ownership; nothing enumerates the conditions the fleet must collectively cover.**

### 1.2 Why a manifest, not a persona update

The naive fix is to extend TIDY's persona to mention local-sweep detection. That works for this one gap. It does not prevent the next gap. A future failure class — say, "extension manifest signature drift after a dependency update" or "trust-graph entry for a peer becomes stale beyond its TTL" — would surface the same way: nobody owns it because nobody has named it.

The manifest decouples the *enumeration of what to watch* from the *assignment of who watches it*. New conditions are added explicitly; assignment is then a separate, reviewable decision; ownerless conditions are themselves an observable condition (the meta-row, §4.2) that prevents silent gaps.

---

## 2. Conformance

A platform conforms to this specification when:

1. A Coverage Manifest exists at a known location (`<extension>/coverage.toml` or `docs/specs/spec-agent-coverage-manifest.md`'s appendix).
2. Each enumerated condition has all five fields populated (§3).
3. Owner agents reference their entries in their `persona.md` or equivalent.
4. The meta-row (§4.2) is present and assigned.
5. Manifest changes follow the amendment process (§6).

Non-conformance is not a runtime failure; it is an architectural smell that the next manifest audit (§5) MUST surface.

The keywords MUST, SHOULD, and MAY in this document are to be interpreted as described in RFC 2119.

---

## 3. Manifest entry schema

Each entry has five required fields:

| Field | Type | Description |
|---|---|---|
| `condition` | string | Human-readable observable condition. MUST be specific enough that detection is mechanical. |
| `owner` | agent name | The agent responsible for detection. Single owner per condition; multi-owner indicates the condition is improperly factored. |
| `detection` | string | The mechanism that surfaces the condition. MUST cite a concrete code path, command, or signal. |
| `response` | string | What the owner does on detection. SHOULD reference the RACI escalation pattern (propose → ask → schedule|back-off|off). |
| `severity` | enum | `info`, `warn`, `escalate`, `block`. Determines downstream consumer behavior (§4.3). |

Example:

```toml
[[condition]]
condition = "Local sweep has ≥10 sustained failures for >24h"
owner     = "TIDY"
detection = "hygiene/local_sweep.py — sample pytest --collect-only + cached pre-push exit"
response  = "Propose chore/ci-flake-cleanup PR via RACI; on user 'yes', spawn cloud routine using state-machine prompt pattern"
severity  = "escalate"
```

---

## 4. Required entries

Every conformant implementation MUST include the following entries (or equivalents). The reference implementation's full manifest lives at `src/axiom/extensions/builtins/hygiene/coverage.toml`.

### 4.1 Baseline conditions

| Condition | Owner | Detection | Severity |
|---|---|---|---|
| Remote CI run failed on watched branch | TIDY | `hygiene/ci_watcher.run_ci_watch_cycle` | `warn` |
| **Local sweep has ≥N sustained failures** | **TIDY** | **`hygiene/local_sweep.py` (new in 0.1)** | **`escalate`** |
| Stale git worktrees on local | TIDY | `hygiene/worktrees.py` | `info` |
| Merged branch / remote ref reclaimable | TIDY | `hygiene/branch_prune.py` (`git branch [-r] --merged`, or a RIVET `rivet.pr_merged` event) | `info` |
| Service unhealthy (DB, LLM server, K3D) | TIDY | `hygiene/manager.py` health check | `escalate` |
| Peer signature change without ratification | TRIAGE | TOFU verification | `block` |
| Test diagnosed as flake (not deterministic bug) | TRIAGE | Triage handoff from TIDY | `info` |
| Sustained failure on `main` blocks release | RIVET | Pre-release gate against manifest | `block` |
| Cohort node silent for >TTL | WARDEN | Federation buddy detection | `escalate` |
| Extension manifest signature invalid | TRIAGE | Sigstore verification at install | `block` |
| Extension capability declared but not provided | TIDY | `axi ext lint` | `warn` |

### 4.2 The meta-row

| Condition | Owner | Detection | Severity |
|---|---|---|---|
| **Failure observed in incident postmortem but no manifest entry** | **TIDY (meta)** | **Coverage audit (§5); `axi hygiene coverage --audit`** | **`escalate`** |

The meta-row is what makes the manifest self-correcting. Without it, the manifest only catches conditions someone thought to enumerate up front; with it, every postmortem-discovered gap becomes itself an observable condition that surfaces to the user via RACI.

### 4.3 Severity semantics

- `info` — Logged. No automatic escalation. Human can query.
- `warn` — Surfaced in next agent heartbeat report. No action required.
- `escalate` — RACI proposal raised to user; user's `[yes / not now / off]` response governs next step. Three `not now` responses suppress further proposals for this condition (per `feedback_raci_automation_escalation.md`).
- `block` — Downstream consumers (especially RIVET release gate) refuse to proceed until the condition clears or is explicitly overridden.

---

## 5. Coverage audits

A coverage audit answers two questions:

1. Has any condition fired since the last audit that does not have a manifest entry?
2. Is every existing entry's owner agent currently responsive (i.e., heartbeating)?

The audit is invoked via `axi hygiene coverage --audit` and SHOULD run on a weekly cadence (or after any incident postmortem). The audit's output is itself an observable condition; if the audit detects an unowned firing, the meta-row (§4.2) escalates it through RACI.

---

## 6. Amendment process

Changes to the manifest follow this process:

1. **Propose** — anyone (human or agent) drafts an entry change.
2. **Review** — owner agent for the affected condition reviews; if no owner exists, TIDY reviews under the meta-row.
3. **Ratify** — change lands via PR with the entry visible in the diff.
4. **Propagate** — owner agent's `persona.md` SHOULD reference the entry; a stale persona is itself a `warn`-severity condition.

There is no quorum requirement. The manifest is an operational artifact, not a constitutional one.

---

## 7. Relationship to AEOS

The Coverage Manifest is **not** part of the AEOS specification (which governs extension shape, signing, and capability declaration). The two specifications compose:

- AEOS describes *what an extension is*. The Coverage Manifest describes *what conditions the fleet of installed agents observes*.
- AEOS conformance does not require Coverage Manifest conformance, and vice versa.
- An AEOS-conformant extension MAY contribute entries to the Coverage Manifest by declaring them in its `axiom-extension.toml` (future work; not in 0.1 of this spec).

---

## 8. Companion specification: cloud-routine prompt pattern

The Coverage Manifest names *what to watch*. When an `escalate`-severity condition triggers a remediation routine, that routine SHOULD follow the state-machine prompt pattern in `spec-cloud-routine-prompt-pattern.md`. The two specs together close the loop: detection (this spec) → routine (the prompt-pattern spec) → resolution.

---

## Appendix A — Lessons from the originating incident

The 2026-05-03 incident produced two structural lessons captured as feedback memory:

- `feedback_state_machine_agent_prompts.md` — cloud routines as state machines, not task lists.
- `feedback_no_prague_as_delay_excuse.md` — root-cause fixes ship now, not "post-deadline."

Both lessons informed this specification's shape.

## Appendix B — Open questions

- **Cross-cohort coverage**: how do federated nodes share manifest entries? An out-of-scope condition on a peer should not be silent locally.
- **Owner-load balancing**: when one agent (currently TIDY) owns disproportionate entries, is there a refactoring threshold?
- **Severity decay**: should `escalate` automatically decay to `warn` after a configured period of quiescence?

These are deferred to a future revision.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
