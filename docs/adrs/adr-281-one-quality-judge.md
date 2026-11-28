# ADR-281: One quality judge

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-135 (fitness and maturity); chunk [C14](https://github.com/b-tree-labs/axiom-os-private/issues/2328) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

Several agents score quality on their own terms, so a promotion can rest on a score nobody else can reproduce.

## Decision (adopted Nov 24 2026, to be proven)

- **CURIO and CHALKE stay separate; CURIO is the one quality judge.** CHALKE, PLINTH and SCAN stop scoring quality on their own and call curio skills.

## Consequences

- C14 (One quality judge) delivers it. Scope: CHALKE, PLINTH and SCAN call curio skills for quality; an eval record is attached to every promotion.
- Retired in the same chunk: The three local quality scorers.

## Proof

- C14: pytest tests/research/test_one_judge.py: a promotion without an eval record is refused; no quality scorer outside research.
