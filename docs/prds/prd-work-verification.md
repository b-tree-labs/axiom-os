# PRD: Work Verification ("Proof-of-Done", program feature 1)

**Status:** Living. **Spec:** `spec-work-verification.md`.
**Program:** feature 1 of the Receipts program.

## Problem

The loudest daily pain in the harness market, in users' own words: an
agent pushing on failing tests ("none of our changes caused this so I
will push"), a demo that "implied success without evidence," an agent
that deleted a production database and faked data. Enterprises say the
same thing formally: the #1 observability gap is intent-vs-execution
divergence. Nobody ships verification of an agent's *claimed work*.

## Users

- A developer who wants to stop re-checking everything the agent says.
- A lead who wants "what did the agents do today" with proof attached.
- The Receipts surface, which renders the resulting task receipts.

## Requirements

### R1 — Claims come from structure, never prose
A "claim" is derived ONLY from structured observation: the tool calls
the platform itself dispatched (a test runner invocation and its exit
artifacts; a file write; a service action; a deploy step). Model text
is never parsed for claims — model-filled fields are untrusted input,
and a verifier built on parsed prose is a verifier built on the thing
being verified.

### R2 — Five claim types at launch, zero config
`tests_pass`, `files_changed`, `command_succeeded`,
`service_restarted`, `deploy_live`. Each has one effect binding (the
fleet evaluator pattern): runner exit codes + counts read from the
runner's own artifacts; file existence + content hash; probe of the
restarted/deployed thing. An open-ended claim grammar is explicitly
deferred — five verifiable types beat fifty mushy ones.

### R3 — Verify-on-read, never block-on-write
Verification NEVER interrupts, gates, or slows the agent. Receipts
accumulate silently; they surface when asked — `axi did`, the Receipts
feed, a session-end summary. A verifier that nags gets bypassed, and a
bypassed verifier is worse than none.

### R4 — VERIFIED / UNPROVEN / CONTRADICTED, face-up evidence
Task receipts carry the observed effect (or its absence). UNPROVEN
(no evidence either way) is distinct from CONTRADICTED (evidence
against — the tests file says 3 failed). Same honesty taxonomy as the
fleet console, same rendering components.

**Verdicts are typed decisions (ADR-126).** Every verdict is a
`TypedDecisionReceipt`: decision type, options, chosen, `decider`
(rule | model | human, with graduation phase for governed seats), and
optional `stated_confidence` where the decider is probabilistic —
absent for the deterministic effect bindings of R2, and the absence
renders as information. Confidence is never evidence (ADR-126 D2): no
confidence threshold substitutes for an observed effect. Where
probabilistic seats participate, the coverage stat (strawman #4's
honest number) gains a calibration column: each seat's
stated-vs-empirical reliability, from the calibration receipt.

### R5 — Per-spawn provenance
Every subagent spawn records goals/scope at dispatch (the market's
explicit ask: "a human-readable audit trail of its goals/intent, its
boundaries"). Receipts attach to the spawn tree.

### R6 — Harness-neutral, launch platforms first
v1 attaches at two seams: the platform dispatch chokepoint (everything
running through Axiom), and session-end hooks for Claude Code and
OpenCode (both expose hooks). macOS and Omarchy are the launch
platforms; nothing in the design may assume systemd or POSIX-only
paths where a portable alternative exists (Windows/legacy-Linux
follow).

### R7 — The Session Card and the Session Page (the flagship visuals)
Every session ends by rendering BOTH:
- **The card**: one beautiful appkit card — claims vs proven, cost,
  spawn count, headline resources. Shareable as a redacted-by-default
  image export (nothing exports without the redaction pass); the
  Wordle-grid-shaped artifact of the guerilla loop.
- **The page**: the card expands to a full page per session — the
  session's complete story: every claim with its verification and
  evidence, the spawn tree with goals/scope, the budget burn, and
  **links to every generated resource** (files written, PRs opened,
  pages deployed, artifacts produced — harvested from the same
  structured observations as the claims, so a link is a fact, not a
  parsed guess). The page is the deep-dive; the card is the glance;
  Today (F0 R11) lists the cards.

## Non-requirements (v1)

- No blocking/enforcement mode (exists later only as opt-in policy).
- No LLM judging of claim text (R1 is absolute).
- No IDE plugins; CLI + Receipts surface + hooks only.

## Red-team

**Steelman.** The felt pain with the largest daily frequency; the
receipts/ledger substrate exists; verify-on-read means zero workflow
friction; the demo ("it said done — here's the receipt") is the
strongest single marketing artifact the program can produce; CI does
not cover it (CI verifies the repo's code; this verifies THE SESSION'S
claims, including non-repo effects: services, files, deploys, local
state).

**Strawman.** (1) Developers bypass anything that adds friction — a
nag dies in a week. (2) Claim extraction is mush: parsing "my tests
pass" from prose is exactly the unreliable layer this product exists
to distrust. (3) "Just look at CI" — leads think they have this.
(4) Coverage honesty: most agent claims won't map to the five types,
so the VERIFIED rate looks sparse and the feature reads as thin.

**Design responses.** (1)→R3 (silent-until-asked is a requirement,
not a preference). (2)→R1 (structured-observation-only is absolute;
prose parsing is a non-requirement). (3)→positioning + the CI
complement sentence in every surface (R4 microcopy). (4)→the coverage
number is rendered honestly as its own stat ("31% of claims
verifiable today") and is the roadmap's growth metric — the same
move as the unproven-green audit: the gap is measured, not hidden.

## Success criteria / kill test

Success: in our own dogfood, ≥1 CONTRADICTED receipt catches a real
false claim within 30 days (the corpus says this is near-certain), and
the session-end summary becomes something we personally read.
Kill: if dogfood shows receipts accumulate and nobody (including us)
ever reads them, the surface is wrong even if the mechanism is right —
redesign the surfacing before building more bindings.
