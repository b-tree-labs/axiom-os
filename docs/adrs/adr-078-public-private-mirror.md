# ADR-078: Public/Private Mirror — how Axiom ships as open source

**Status:** Accepted
**Date:** 2026-06-22
**Related:** ADR-032 (standards positioning, dual-track), ADR-048 (brand-scoped extension visibility), [[NOTICE]]

## Context

Axiom is a domain-agnostic, open-source agent platform. Most of the repository
is public-safe once incidental institution/domain references are demoted to
labeled generic examples (the genericization sweep; see the public-readiness
work). A small residue, however, *cannot* be meaningfully genericized in place
because its institution- or domain-specific content **is** its substance:

- **Strategy & scratch** — `docs/working/` (session checkpoints, brand/market
  strategy, ownership map) and other in-flight planning notes.
- **Domain artifacts** — e.g. a Vim syntax file for a nuclear transport code,
  a real-deployment LMS case study, nuclear-physics RAG/eval corpora, and the
  export-control classifier's regression fixtures.

Gutting these would destroy the artifact; keeping them in a public repo leaks
strategy, a real deployment, or names we don't want headlining an OSS harness.

## Decision

Adopt a **public/private mirror** model (the same pattern used by
Postrule / Postrule-Private):

0. **Naming convention** — public is the default, so the public mirror takes the
   bare name (`b-tree-labs/axiom-os`) and the private source-of-truth carries the
   `-private` suffix (`b-tree-labs/axiom-os-private`). Public-facing docs and URLs
   reference the bare (public) name.
1. **Source of truth stays private** — development continues in the private
   `axiom-os-private` repository, which contains everything.
2. **The public repository is a generated mirror** — a publish step copies the
   repository minus the paths listed in **`mirror/exclude.txt`** (gitignore
   syntax) into the bare-name public repo. Nothing in the public tree is
   hand-maintained separately.
3. **Genericize-in-place is the default; exclude is the exception.** A path is
   added to `mirror/exclude.txt` only when it cannot be genericized without
   losing its meaning. Everything else is made domain-neutral in the live repo
   so the public and private trees stay byte-identical for shared files.

### Excluded-set rationale

`mirror/exclude.txt` is the authoritative list. Each entry is there because the
content is strategy/scratch, a real deployment, or a domain corpus whose terms
are the test fixture itself.

### Dependent tests

Some excluded fixtures back unit/eval tests (`tests/promptfoo/*.yaml`,
`tests/routing/public_prompts.txt`). Those test files must either ship a
genericized public stand-in fixture or be excluded alongside their data so the
public test suite stays green. Tracking item — not resolved by this ADR; the
publish step must verify the public tree's tests pass before pushing the mirror.

## Consequences

- **Pro:** one source of truth; no divergent public fork to maintain; the
  exclude list is small, reviewable, and explains *why* each path is private.
- **Pro:** the genericization discipline keeps the excluded set from growing —
  new domain leakage is a code-review smell, not a mirror-rule addition.
- **Con:** the publish tooling (the actual mirror script + CI) is not built
  here; this ADR defines the contract (`mirror/exclude.txt` + "public tree must
  be green") that tooling will implement.
- **Con:** excluded fixtures create a follow-up obligation to provide public
  stand-ins or exclude dependent tests.
