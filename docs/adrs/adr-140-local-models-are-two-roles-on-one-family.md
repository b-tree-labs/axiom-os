# ADR-140 — Local models are two roles on one family

**Status:** Accepted
**Date:** 2026-09-28
**Supersedes:** ADR-054's model resolutions (its tier-policy primitive stands)
**Related:** ADR-054 (LLM-tier policy), ADR-139 (a skill accepts the caller's goal)

## Context

ADR-054 made tier policy a first-class primitive so that changing which model
a tier resolves to would be "one declarative file update" rather than N edits.
The primitive was built. The resolutions were then hardcoded anyway:
`llama3.2:1b` appears as a literal default in eight places — the router, the
routing-health probe, `axi doctor`, the settings store, the chat connection
helper, the SLM advisor, and the status CLI.

Two problems followed.

**The defaults are not Apache-2.0.** `llama3.2:1b` is under the Llama 3.2
Community License and `gemma2:2b` under the Gemma Terms. The standing rule
for this platform is no dependency more restrictive than Apache-2.0. A model
pulled at runtime is not a bundled library, but a *default* is what everyone
gets without choosing, and it is what an open-sourced extension ships.

**Size was treated as a dial when it is a job boundary.** ADR-054 put
`gemma2:2b` at the "operational/housekeeping" tier and `qwen2.5:7b` at the
"classroom" tier, framing the small model as the same capability in a smaller
box. Measured on a real task on 2026-09-28 — explaining a drift finding whose
answer required joining a finding to a worktree listing and a lane record:

| model | latency | outcome |
|---|---|---|
| `gemma2:2b` | 4.3s | missed the cause; invented a shell command |
| `phi3.5:3.8b` | 6.0s | missed the cause; invented a shell command |
| `qwen2.5:7b` | 13.0s | named the finding **and** inferred the deletion |

The 2B models were not a cheaper version of the right answer. They were a
fast wrong one, which is worse, because fluent and wrong reads as certain.

## Decision

**Two roles, one family, both Apache-2.0.**

1. **quick** — classification, a one-line next step, terminal affordances.
   Resolves to **`qwen2.5:1.5b`**. Sized for latency a person should not
   perceive. Replaces `llama3.2:1b`, and is smaller on disk as well as
   cleanly licensed.
2. **reasoning** — an inference across sources that no single source states.
   Resolves to **`qwen2.5:7b`**. Replaces `gemma2:2b` at the housekeeping
   tier; already ADR-054's classroom leaf, so that half is unchanged.

**One family.** Both are Qwen 2.5: one set of prompt quirks, one licence to
track, one vendor to follow. `qwen2.5:3b` is deliberately excluded — it is
the single model in the series under the Qwen RESEARCH licence, and it is
exactly the size an optimiser would reach for.

**Resolved from one place.** `axiom.llm.local_models` holds the roles and
their defaults; every call site reads from it or from the setting it backs.
The eight literals are the defect ADR-054 set out to prevent, and leaving
them while changing the values would guarantee they diverge.

**Gemma and Phi remain available, not default.** Both are selectable by
setting. Gemma is the right answer where 1.6GB is the binding constraint and
the job is quick-tier.

## Consequences

- Deployments pinning `routing.ollama_model` are unaffected; only the
  fallback moves.
- Anything that has only ever pulled `llama3.2:1b` must pull `qwen2.5:1.5b`.
  `axi doctor` reports a configured-but-absent model already.
- The reasoning default is 4.7GB, up from 1.6GB. That is the real cost of
  this decision and it is accepted deliberately: the job is invoked when
  something is already broken, where 13 seconds and a correct answer beats 4
  seconds and a confident wrong one.
- `llamafile.py`'s `small` profile keeps Gemma, now documented as a footprint
  choice rather than a tier. There is **no Apache-licensed small profile in
  that registry**; the quick tier is served through Ollama. Closing that gap
  needs a verified single-file GGUF URL.
- **A small model will confabulate a command when asked for one.** All three
  models tested did, including the 7B. Callers must offer fixes to choose
  between rather than requesting one — see `lane`'s `OFFERED FIX` prompt.
