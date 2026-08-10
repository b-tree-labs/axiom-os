# PRD — REV-U: Automated PR Review Agent

**Status:** Active  
**Owner:** B-Tree Labs  
**Version:** 0.1  
**Date:** 2026-05-02

---

## Problem

Code review is the highest-leverage quality gate in software development, but it is also the most consistently under-resourced. Reviewers scan large diffs quickly, miss subtle security issues, and apply inconsistent standards across time. This is compounded in a fast-moving platform like Axiom where multiple contributors work across memory, federation, extension, and CLI layers simultaneously.

No automated tool today provides multi-pass, context-aware review tuned to Axiom's domain vocabulary, architectural invariants, and quality standards.

---

## Goal

Build REV-U — a local-diff PR review agent that runs 5 specialized review passes against any `git diff` output, validates findings for hallucination, and surfaces a ranked finding list in the terminal (or as JSON for CI pipelines).

REV-U is Phase 1 of a multi-phase roadmap. Phase 1 is entirely local — no GitHub integration, no memory persistence, no plan-first mode.

---

## Phased Roadmap

| Phase | Scope | Status |
|---|---|---|
| **1 — Local diff** | `git diff`, 5 passes, CLI output | **This PRD** |
| 2 — GitHub PR ingestion | Fetch PR by number, review remote diff | Deferred |
| 3 — Inline comment posting | Post review comments via GitHub API | Deferred |
| 4 — Memory / Learnings | Learn from accepted/rejected findings | Deferred |
| 5 — Plan-first review | Generate a review plan before running passes | Deferred |
| 6 — Linus-mode retraction | High-confidence retraction of bad findings | Deferred |
| 7 — Sandbox test execution | Run tests in sandbox, surface failures | Deferred |

---

## Phase 1 Scope

### Extension

AEOS-conformant builtin at `src/axiom/extensions/builtins/review/`.

### Agent: REV-U (agents/rev_u/)

- Accepts a unified diff string + repo root
- Runs 5 specialized passes in sequence via injected LLM
- Aggregates and deduplicates findings
- Passes an optional `--pass` filter to run only a subset

### Tools

| Module | Exports | Purpose |
|---|---|---|
| `tools/diff.py` | `local_diff(base)` | Shell out to `git diff <base>...HEAD --unified=5` |
| `tools/context.py` | `gather_context(diff, repo_root)` | Read full file contents for diff-touched files |
| `tools/findings.py` | `Finding`, `FindingSet` | Core data types |

### Five Review Passes

Each pass runs independently with a tightly-scoped system prompt. Passes return `list[Finding]`.

| Pass | `pass_kind` | Focus |
|---|---|---|
| Correctness | `correctness` | Logic errors, type mismatches, invariant violations |
| Performance | `performance` | Algorithmic complexity, N+1, unnecessary allocations |
| Security | `security` | Injection, secrets, auth, input validation, OWASP |
| Docs | `docs` | Missing docstrings, stale comments, API contract drift |
| Tests | `tests` | Missing test coverage, untested edge cases, test quality |

### Validator

Second-pass gate that drops:
- Findings whose `path:line` does not appear in the diff (hallucination prevention)
- Nit findings beyond the 20-nit noise floor
- Line tolerance: ±2 lines (LLM may cite slightly off)

### CLI: `axi review`

```
axi review [--base <ref>] [--severity <level>] [--pass <kind>] [--json] [--no-validator]
```

- `--base`: git ref to diff against (default: `main`)
- `--severity`: minimum severity to show (default: `minor`)
- `--pass`: repeatable, filter to specific pass kinds (default: all 5)
- `--json`: emit machine-readable JSON to stdout
- `--no-validator`: skip validator (escape hatch)

Exit codes: 0 = no blockers, 1 = blockers found.

### Terminal Report Format

- Group by severity, then pass_kind, then file
- Colors: blocker=red, major=yellow, minor=cyan, nit=dim
- Footer: `N findings (Bb · Mm · mn · n nits) across F files · ~Ts`
- Uses `axiom.infra.text_utils` (pluralize, bar, header)

---

## LLM Tier

Uses `standard` tier per LLM-tier policy. Resolution via `axiom.policy.llm_tier` (stub if not yet available; graceful fallback to `axiom.infra.gateway.Gateway`).

---

## Tests

32+ tests in `src/axiom/extensions/builtins/review/tests/`. All tests use `unittest.mock` — no real LLM calls.

---

## Acceptance Criteria

1. `pytest src/axiom/extensions/builtins/review/tests/ -q` → 30+ passed, 0 failed
2. `ruff check src/axiom/extensions/builtins/review/` → clean
3. `axi review --help` renders without crash
4. `axi review --base HEAD~1` returns a report (stub LLM if no key configured)
5. Draft PR open on `b-tree-labs/axiom-os`

---

## Phase 2 — GitHub PR ingestion

**Status:** Pending; this PRD now scopes Phase 2 in detail (post-Phase-1 ship).

The Phase-1 surface reviews a *local* `git diff`. Phase 2 lifts that constraint: review any PR by number, against its own base ref, without requiring a local checkout.

### Phase 2 scope

- New flag: `axi review --pr <number>` (mutually exclusive with `--base`).
- `gh pr view <number> --json baseRefName,headRefName,headRepository` resolves the diff coordinates without cloning.
- Diff fetched via `gh pr diff <number>` (pure stdout; no working-tree mutation).
- Report format unchanged from Phase 1; the JSON output adds `pr_number` and `pr_url` fields.
- `--repo <owner/name>` flag supports cross-repo review (e.g. reviewing a fork's PR against an upstream).

### Why this matters for the queue-piling-up problem

When the local PR queue is large (today: 7 open across `b-tree-labs/axiom-os` after the 2026-05-04 push), reviewers can't realistically check out and locally review each PR. Phase 2 makes the review agent the first-pass triager: run `axi review --pr <n>` for each open PR, get a ranked findings list per PR, decide which need deep human review and which can be merged on the strength of CI plus the agent's pass.

### Phase 2 acceptance

1. `axi review --pr <n>` returns the same shape of report as `axi review --base <ref>`
2. Works against open, draft, and recently-closed PRs
3. Cross-repo support verified against an upstream/fork pair
4. CI smoke test: `axi review --pr <n> --json` produces a valid JSON envelope

---

## Phase 3 — Inline comment posting

**Status:** Pending.

After Phase 2 generates findings, Phase 3 closes the loop by posting them as PR review comments via the GitHub API.

### Phase 3 scope

- New flag: `axi review --pr <n> --post` (default: dry-run preview).
- Each finding becomes a single inline comment at its `path:line`. Severity prefix in the comment body (`[BLOCKER]`, `[MAJOR]`, `[MINOR]`, `[NIT]`).
- A summary review-level comment lists the count by severity and links to the agent's full report.
- Findings posted as a single review (one API call) so the PR conversation gets one batch, not 30 individual notifications.
- The comment author is the user's gh-CLI identity; the agent does *not* impersonate or post anonymously.
- Idempotency: a second `--post` on the same PR with no new findings is a no-op (deduplicated against existing review comments by content hash).

### Phase 3 risks

- **Spamming reviewers** — mitigated by the dry-run default and the 20-nit noise-floor cap.
- **Over-asserting findings** — every comment carries the validator's confidence in its body; `[BLOCKER]` requires `confidence ≥ high` (validator threshold tuned against a labelled corpus before Phase 3 ships).
- **Posting to the wrong PR** — `--pr <n>` is required; no inferred-default PR target.

### Phase 3 acceptance

1. Dry-run preview matches the eventual `--post` output verbatim
2. `--post` produces exactly one PR review with N inline comments matching the findings
3. Re-running `--post` on an unchanged diff posts nothing new
4. Per-finding confidence is visible in the rendered comment body

---

## Phase 4 — Memory / Learnings

**Status:** Pending.

The agent learns from accepted/rejected findings to improve precision over time.

### Phase 4 scope

- Each posted finding produces a memory fragment via the existing CompositionService — `(T, U, A, R) = (timestamp, user, review-agent, finding-id)`.
- When a reviewer reacts to a comment (👍 / 👎 / "resolved" / "outdated"), the reaction routes back into the fragment as an outcome label.
- Fragments accumulate per (pass-kind, file-glob, finding-pattern). After ~50 labelled instances per cluster, the agent's prompt for that cluster is amended with a few-shot example block: high-precision past findings + their accept/reject outcome.
- Fragment-derived prompt amendments are session-local first, then promoted to repo-default after `K` consecutive precision wins.
- Memory is per-(user, repo); does not cross between repos without explicit promotion.

### Phase 4 dependencies

- ADR-035 LLM-tier policy (already on main)
- ADR-027 federated memory (already on main)
- ADR-043 RACI evolution (lands alongside this expansion) — promotion of an amended prompt from session-local to repo-default routes through the graduated-autonomy state machine; first promotion is `ASK`, subsequent ones can advance to `AUTO` per D1 of ADR-043.

### Phase 4 acceptance

1. Findings produce CompositionService fragments
2. Reviewer reactions update the fragment outcome label
3. Prompt amendments are visible in `axi review --explain` for the affected cluster
4. Promotions to repo-default are gated through RACI per ADR-043

---

## Phase 5 — Plan-first review

**Status:** Pending.

For PRs over a complexity threshold, the agent generates a *review plan* before running the 5 passes. The plan describes which files matter most, which architectural invariants are at risk, and which passes to run with elevated severity.

### Phase 5 scope

- Triggered automatically when the diff exceeds `--plan-threshold N` lines (default `N=200`).
- Plan generated by the `smart` tier (one tier above the standard pass tier).
- User can approve, reject, or edit the plan before passes run.
- An approved plan is recorded to the memory ledger for the (repo, file-glob) cluster; future PRs of similar shape can re-use prior plans (subject to RACI evolution per ADR-043).
- A rejected plan does not prevent review — passes run with default config.

### Phase 5 acceptance

1. PRs above threshold trigger the plan prompt
2. Plan is displayed in the terminal with a single-key approve/reject/edit interface
3. An approved plan visibly affects the per-pass severity weighting
4. Plan re-use is gated through the RACI ledger

---

## Phase 6 — High-confidence retraction

**Status:** Pending.

The validator from Phase 1 catches *hallucinations* (findings whose path:line is not in the diff). Phase 6 catches a more subtle failure: findings that *are* about real code in the diff but are nonetheless wrong on the merits — incorrect security claims, misread call patterns, mistaken type inferences.

### Phase 6 scope

- After the validator runs, a separate "retraction" pass re-evaluates the surviving findings against a stricter prompt: "for each finding, find a counter-argument; if the counter-argument is stronger than the finding's claim, retract."
- Uses the `smart` tier; smaller context window per finding (just the finding + surrounding 50 lines + counter-argument prompt).
- A retracted finding is removed from the report with a single-line note: `[retracted] <pass>: <original-claim> — <counter-argument summary>`.
- Aggressive: the bar for retraction is "any non-trivial counter-argument," not "fully refuted." Better to under-call than over-call.
- Configurable via `--no-retraction` to disable.

### Phase 6 risks

- **Over-retraction** of correct findings — measure precision drop on a labelled corpus before shipping; require `< 5%` true-positive retraction rate.
- **Cost** — adds a per-finding LLM call. Bounded by the validator+retraction together producing a finding rate `≤ pass output finding rate`.

### Phase 6 acceptance

1. Findings carry a retraction state (kept / retracted) in the JSON output
2. Retraction reasons are surfaced in the report
3. Labelled-corpus precision drop measurement is in `axi review --benchmark`

---

## Phase 7 — Sandbox test execution

**Status:** Pending; final phase before this PRD considers the agent feature-complete.

Findings sometimes claim "this would break the test suite." Phase 7 lets the agent verify that claim by actually running the relevant tests in a sandboxed checkout.

### Phase 7 scope

- For each `[BLOCKER]` finding that names a test, the agent fetches the diff (Phase 2), applies it to a sandbox checkout, runs the named test, and confirms (or refutes) the breakage claim.
- Sandbox runs in the per-language attestation surface (ADR-039 D3 / ADR-036).
- If the test passes despite the finding's claim, the finding is downgraded to `[MAJOR]` with the note `verified: test passes`.
- If the test fails as predicted, the finding is upgraded with the note `verified: test fails as claimed`.
- Bounded runtime: each verified test must complete in `< 60s` or the verification is skipped (and the finding stays at original severity).

### Phase 7 dependencies

- Phase 2 (PR ingestion) — required for sandbox-applicable diffs
- Sandbox infra (ADR-036 / ADR-039 D3) — required
- ADR-040 (compute decomposition) — *optional*; if available, multiple per-finding test runs can decompose across federation peers, reducing per-PR review time

### Phase 7 acceptance

1. Blocker-with-test findings carry a verification status
2. Sandbox runs do not affect the user's working tree
3. Verified test runs complete within the 60s bound or are explicitly skipped
4. JSON output exposes verification state for CI consumers

---

## Cross-cutting: agent naming and the Pixar-IP-remediation policy

Per `feedback_walle_ip_risk_pre_launch` and the rename memory (`project_walle_rename_decisions`), the agent fleet is dropping dash-letter Pixar typography (`WALL-E → AXI`, `CURI-O → CURIO`, `CHALK-E → CHALKE`, `V-EGA → WARDEN`; open candidates SCAN / TIDY / PRESS / TRIAGE for the rest). The shorthand "REV-U" carries the same dashed-letter signature.

The current code uses `RevUAgent` (camelcase, dash-free) and the CLI surface is `axi review` (no `rev-u` anywhere user-visible), so user-facing exposure is minimal. The PRD title and filename retain "REV-U" pending an explicit rename pick.

**Open decision before public launch:** whether the agent's short name retains "REV-U" / "REVU" or adopts a wholly new identifier consistent with the SCAN/TIDY/PRESS/CHALKE family. This PRD does not pre-empt that decision; whatever name is chosen, the phased surface above applies. Track the decision in `project_walle_rename_decisions` memory.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
