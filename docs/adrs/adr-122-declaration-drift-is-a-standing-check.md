# ADR-122: Capability declaration drift is a standing check, not a review item

**Status:** Proposed
**Date:** 2026-09-21

## Context

A capability registered with `registry.register(name, fn)` works from the CLI
and is invisible to everything else. It carries no description and no declared
surfaces, so it cannot appear in an MCP tool list, the agent tool loop,
`SKILL.md`, or the capability discovery block.

That shorter form is what most examples use, so it is what gets written. A
measurement found **58 of 117 registered capabilities undeclared** — whole
namespaces, including every `secrets.*`, `schedule.*` and `release.*` verb.

The cost is not theoretical. An assistant asked to handle an expiring credential
could not learn that `axi vault` or the thirteen `secrets.*` verbs existed. It
reached for a raw git credential helper, which is keyed by host alone, got the
wrong one of the eight tokens on that host, and concluded there was no working
credential — while a healthy one sat in the store under another name. The
rotation pass that should have prevented the expiry was itself undeclared, so
nobody could be offered it either, and it had been crashing for weeks.

Two mechanisms were considered and neither is sufficient alone:

- **Code review.** Nobody reads a `skills/__init__.py` diff asking "should this
  have declared surfaces?", and the answer depends on knowledge of a projection
  system the author may not have met.
- **A ratchet on the count.** Necessary, and it teaches nothing. The next author
  writes the same line, discovers the failure at review, and learns a rule
  rather than a reason.

## Decision

**Declaration drift is a standing node-health check that proposes its own fix.**

1. `axi hygiene stat capabilities` reconciles what is REGISTERED against what is
   DECLARED across every installed extension, and emits a **paste-ready
   `SkillSpec`** per gap.
2. The same check runs as a `Finding` in `audit_node()`, so it rides the
   `hygiene stat health` heartbeat. hygiene is deliberately the host: it is the
   drift surface, and in practice the one daemon operators consent to run. A
   check on a disabled daemon is the failure this came from.
3. The report is **educational per capability**, not as doctrine. It names the
   tool the capability would become (`axiom_connector__status`), the surfaces it
   would reach, and the gates it becomes eligible for — telemetry, ADR-114
   authority rules, `SKILL.md` generation.
4. A ratchet (`MAX_UNDISCOVERABLE`) holds the floor, and fails loudly when the
   count *falls* so the gain is locked in rather than drifting back.

Three rules the mechanism does not bend:

- **It never applies anything.** Editing an extension because a heuristic
  inferred a description is the same drift arriving from the other direction.
- **It never invents a description.** An undocumented capability is reported as
  blocked on one. "Run demo.mystery" teaches nothing, looks authored, and would
  never be fixed because it no longer looks broken.
- **It never proposes MCP for a mutating verb.** A write on a protocol surface
  is a decision somebody makes. Accepting every suggestion verbatim must not be
  able to widen an effect nobody chose. Where a verb *looks* read-only but is
  registered mutating (the `register()` default), it raises a **hint** naming the
  one-word change — a question, not a correction.

## Consequences

**Good.** The gap is measured rather than asserted, and the fix arrives with the
finding. Every promotion moves a capability into reach of every surface at once,
which is also what feeds the discovery block, so the effort compounds. New
extensions meet the rule as a suggestion on their first health report rather
than as a review comment.

**Cost.** `audit_node()` gains an introspective check, so it must be injectable
to stay hermetic — `capability_registry` joins `version_checker` and
`directive_store` for the same reason. The check is WARNING, never CRITICAL: an
undeclared capability still works for anyone who knows the verb, and grading it
CRITICAL would train people to skim the report.

**Accepted risk.** Name-based hints can be wrong. They are therefore never
applied and never widen a proposal; the worst case is a question an author
answers with "no".

**Not addressed here.** Four extensions still bypass `invoke_capability`
entirely (tracked as `KNOWN_BYPASSES`, shrink-only). A capability can be declared
and still not reach the dispatch chokepoint; these are separate defects.
