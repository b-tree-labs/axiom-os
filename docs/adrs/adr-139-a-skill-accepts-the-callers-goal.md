# ADR-139 — A skill accepts the caller's goal

**Status:** Accepted
**Date:** 2026-09-28
**Supersedes:** none
**Related:** ADR-056 (CLI verbs are thin wrappers over skills), ADR-063
(SkillSpec), ADR-046 (RIVET/TIDY boundary), ADR-074 (principal in context)

## Context

Every capability in this platform is a skill: `(params, ctx) -> SkillResult`,
surfaced as a CLI verb, an MCP tool and an agent tool from one registration.
The same function answers a person at a terminal and an orchestrating agent
three layers up.

Those two callers want different answers to the same call. A person running
`lane doctor` reads a list and picks. An orchestrating agent that called it
because it was trying to start an app does not want a list — it wants the one
finding that explains why the app will not start, and it pays for everything
else in context it cannot spare.

Today a skill cannot tell the difference, because nothing in the call says
what the caller was trying to do. `SkillContext` carries **who** is acting
(`principal`) and `ActionIntent` names **what action** is being authorised.
Neither carries **why the caller is asking**, and a skill that does not know
why is reduced to returning everything and hoping.

This matters more as these skills are invoked through MCP from other agentic
environments. A tool that returns forty findings to a parent agent has not
answered a question; it has delegated the question back, having spent the
parent's context to do so.

## Decision

**Every skill accepts an optional `caller_goal`: one short sentence, in the
caller's own words, saying what they are trying to accomplish.**

1. **It is a parameter, not context.** It varies per call, the way `source`
   or `table` does. `SkillContext` carries what is true about the invocation
   — who, where, which registry. What the caller wants is an input.

2. **It is named `caller_goal`, not `intent`.** `ActionIntent` is an
   established type in `axiom/governance/intent.py`, used across fourteen
   files as a machine-readable action identifier for authorisation and
   receipts. Two meanings for one word is how a citation becomes ambiguous;
   we have paid for that twice this month in ADR numbers alone. `goal` and
   `objective` are already in use for unrelated things. `caller_goal` is
   free and says whose goal it is.

3. **It is always optional and never load-bearing.** A skill must return a
   correct, complete answer when it is absent — that is the CLI case and the
   CI case. `caller_goal` may change emphasis, ordering, verbosity and what a
   summary leads with. It may **not** change what is true, what is checked,
   or what is permitted. Authorisation reads `ActionIntent` and the
   principal, never this.

4. **It is declared in `SkillSpec.inputs`** so it reaches the generated
   SKILL.md and the MCP tool schema mechanically. A parameter a caller cannot
   discover is a parameter nobody passes; this is the difference between a
   convention and a contract.

5. **It is untrusted text.** It arrives from a caller, frequently a model. It
   is prose for shaping a response and is never interpolated into a query, a
   path, a command or a permission decision.

## Consequences

- Skills that surface a list gain a reason to lead with something. An MCP
  tool can return a short answer plus structured detail, rather than a table
  the parent must re-reason over.
- Local small-model reasoning (the Ollama advisor pattern) gets the one input
  that makes it useful rather than decorative: not "explain these findings"
  but "explain these findings to somebody trying to start the chat app".
- A skill that ignores `caller_goal` stays correct. Adoption is per-skill and
  needs no coordinated migration.
- The generated SKILL.md files change shape once, when the field is added to
  the spec.
- **`caller_goal` must never appear in an audit record as the reason an
  action was permitted.** It is the caller's description of their own
  purpose, which is exactly the thing an audit trail may not take on trust.
  Receipts record `ActionIntent` and the principal.
