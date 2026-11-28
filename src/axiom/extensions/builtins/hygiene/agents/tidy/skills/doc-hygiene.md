# Skill: doc-hygiene

TIDY audits a repository's `docs/` tree against the portfolio documentation
standard (`docs/conventions/doc-standards.md`) and proposes fixes. Detection
is deterministic (`axi hygiene stat docs`); relocation and renaming are
git-visible, reviewable changes that route through propose → approve — TIDY
never moves a document on his own authority.

## When to use

- The heartbeat repo-hygiene sweep, on every repo TIDY stewards.
- After a PR merges that added files under `docs/`.
- On demand: an operator asks whether a repo's docs conform.

## Contract

1. **Detect deterministically.** Run `axi hygiene stat docs --repo <path>
   --json`. The checks: loose files at the docs root, kind-prefix
   violations in `prds/` / `specs/` / `adrs/`, non-kebab filenames, ADR
   number collisions, retired folder names (`requirements/`, `tech-specs/`),
   missing H1s, broken relative links. Never hand-roll a variant of these
   checks in prose — if a rule is missing, it belongs in `doc_hygiene.py`
   with a test that can fail.
2. **Report, then propose.** Findings surface as INFO/WARNING through
   `node_health`. For each actionable finding, propose the specific `git mv`
   / rename / link-fix batch with the finding cited as evidence. Moves are
   `reversible=True` (git history preserves the old path).
3. **Never touch content.** This skill relocates and renames; it does not
   edit document bodies (the one exception: rewriting references to paths
   the proposal itself moves). ADRs are immutable — a misplaced ADR moves,
   its text never changes.
4. **Scratch is exempt.** `working/`, `_archive/`, and underscore
   directories are out of scope by design; do not propose tidying them.

## Escalation

A finding that needs a judgment call — a document whose kind is genuinely
ambiguous, a collision where both ADRs are cited elsewhere — is surfaced to
the operator with the options stated, not resolved silently.
