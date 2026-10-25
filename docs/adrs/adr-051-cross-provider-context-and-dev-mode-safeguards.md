# ADR-051 — Cross-provider project context + dev-mode safeguards

**Status:** Proposed (2026-05-29) · Scope: Axiom and the first consumer repo (the domain consumer) for now; generalize to the rest of the portfolio later

## Context

Two recurring foot-guns slow down work in this codebase, and both will hit
external Axiom-extension developers exactly as they hit us:

**1. Multiple AI coding assistants, one set of project rules.** The real
working environment uses Claude Code, JetBrains (Junie), Codex, and Cursor
against the same repos. Each tool reads a *different* context file. Today
Axiom keeps `CLAUDE.md` as a symlink to **`AGENTS.md`** — so AGENTS.md is
already the de-facto canonical file, and the tools that read AGENTS.md
natively (Claude Code via the symlink, Codex, Cursor) are covered. The gap is
the tools that read neither AGENTS.md nor a symlink: Cursor's richer
`.cursor/rules/*.mdc`, JetBrains **Junie** (`.junie/guidelines.md`), and
GitHub Copilot (`.github/copilot-instructions.md`). Keeping those hand-synced
guarantees drift — and a stale rule file is worse than none, because it lies
with authority.

**2. "Running it while developing it" is confusing.** Developing a consumer extension (or
an Axiom extension) while a local daemon runs the same code has repeatedly
produced "I changed the code but nothing happened." This is **two distinct
problems** wearing one coat:

- *Install lag.* A **non-editable** install copies code into `site-packages`,
  so edits to `src/` never take effect. (This bit us this session: the running
  RIVET daemon would not have picked up a new feature until reinstall; pytest
  saw `src/` because it is on the path, a bare `python -m` / the daemon did
  not.)
- *Process lag.* Even with an **editable** install, a long-running daemon holds
  its imported modules in memory until restarted, so a code change needs
  `axi agents restart` to take effect.

The detection primitive for #2 already exists: `release/mode.py`'s
`detect_mode()` distinguishes **developer** (editable) from **operator**
(installed). The problem is that this state is *silent* — nothing surfaces it
at the moment of confusion.

This ADR does **not** address runtime conversational memory — the
`axiom-memory` MCP ledger already federates session recall across tools. That
is a different layer (*session recall*); this ADR is about *static project
rules* and *dev-environment legibility*.

## Decision

### A. AGENTS.md is the single canonical project-context file, per repo

- Each repo (Axiom, the domain consumer) has exactly one hand-authored context file:
  **`AGENTS.md`**. `CLAUDE.md` remains a symlink to it. AGENTS.md aligns with
  Axiom's existing AAIF public-contribution posture (ADR-032).
- Every other tool's context file is **generated**, never hand-edited.

### B. `axi context sync` fans AGENTS.md out to the per-tool formats

- A new Axiom command generates, from the canonical AGENTS.md:
  - `.cursor/rules/` (Cursor MDC rule(s)),
  - `.junie/guidelines.md` (JetBrains Junie),
  - `.github/copilot-instructions.md` (Copilot).
- Every generated file carries a header: *"GENERATED from AGENTS.md by
  `axi context sync` — do not edit."*
- A **pre-commit hook + CI check** (`axi context check`) fails when a generated
  file drifts from what AGENTS.md would produce. Drift is a build break, not a
  silent inconsistency.
- Generation (not symlinks) is the cross-portfolio mechanism because symlinks
  do not survive Windows checkouts or some tooling — consistent with the
  cross-platform support matrix. The `CLAUDE.md → AGENTS.md` symlink stays only
  because it already works for Claude Code on the dev platforms in use.
- This is an Axiom platform capability that the domain consumer (and later the rest of
  the portfolio) consumes by running the same command — it dogfoods Axiom and
  is itself AAIF-contributable.

### C. Make dev-mode state loud (the safeguards)

- **Bootstrap defaults to an editable install** for development setups, so
  *install lag* (problem #2a) does not occur by default.
- **Surface `detect_mode()` + a stale-code check.** Stamp the installed
  revision at install time; `axi agents status` / `axi doctor` compares it to
  the working-tree `HEAD` and prints a one-line banner when they differ:
  *"daemon running `<sha-a>`; working tree `<sha-b>` — `axi agents restart` to
  sync."* That single line explains both the consumer-side confusion and a stale
  daemon, at the moment it matters.

### D. Adoption path — how a repo opts in (no new workflow)

Adoption reuses Axiom's two existing onboarding idioms; a repo learns nothing
new:

- **Scaffold (`axi context init`)** — models `axi ext init`. For a fresh repo:
  writes a starter canonical `AGENTS.md`, creates the `CLAUDE.md → AGENTS.md`
  symlink, runs the first generation, and installs the pre-commit hook. Zero →
  all-tools-covered in one command. Idempotent: re-running never clobbers an
  existing hand-authored `AGENTS.md`.
- **Step-runner (`axi install`)** — for managed environments, a `context-sync`
  step (and an `editable-install` step) lives in the repo's install manifest.
  `axi install` is already idempotent + state-tracked, so a new clone runs the
  same command everyone runs and gets the capability for free.
- After adoption the **pre-commit hook + CI drift check** keep it current, so
  it cannot silently rot, and a later `axi context sync` that gains a new tool
  format upgrades the repo on its next run.

### E. Discovery path — how Axiom surfaces "this repo could upgrade"

Discovery uses Axiom's proactive Finding-surfacing idiom, **not** a
wait-for-audit (hygiene surfaces before accumulation):

- **Capability gate (ADR-047 `requires`)** is the present/absent primitive:
  is the hook installed, do the generated files exist, is the install editable.
- **A `doctor` / TIDY check** emits a `Finding(severity, message, fix)` on the
  heartbeat, aggregated to AXI. The check fires when: `AGENTS.md` exists but a
  generated target is missing; a generated file has drifted; or a dev workspace
  is on a non-editable install.
- **Every Finding carries its own remediation** — `→ run 'axi context init'` /
  `'axi context sync'` / `'axi agents restart'`. Discovery and the fix arrive
  together, so "upgrade a repo" is a one-liner the operator can run on sight.
- The dev-mode banner from (C) lives in `axi doctor` for the same reason — it
  is the health/audit surface.

### F. Scope

Axiom and the domain consumer only, for now. Other portfolio repos adopt once the
command and the safeguard are proven in these two.

## Consequences

**Positive**
- One file to maintain; every tool stays consistent or CI fails.
- New contributors and external extension developers get a legible
  environment: their assistant has the rules, and "why didn't my change take"
  has an answer printed on screen.
- Reuses what exists (AGENTS.md canonical, `mode.py` detection) rather than
  inventing a parallel system.

**Negative / costs**
- A generator and per-format templates to build and maintain; each new tool
  format is incremental work.
- Generated files add tracked artifacts to each repo (mitigated by the
  do-not-edit header + drift check).
- The stale-code banner needs an install-time revision stamp, which the
  bootstrap / `axi update` path must write.

**Neutral**
- The `axiom-memory` runtime ledger is unaffected and remains the separate
  session-recall layer.

## Open questions / follow-ups

- One Cursor MDC rule vs. several scoped rules (globs)?
- Should generated files be `.gitignore`d and produced on checkout instead of
  committed? (Trade-off: clean tree vs. zero-setup for someone who never runs
  `axi context sync`.) Default here is *committed + drift-checked*.
- Where the install-revision stamp lives for an editable install (no wheel
  metadata) — likely a small file written by bootstrap.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
