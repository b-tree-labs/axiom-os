# ADR-211: The agent-activity verb is Control (provisionally), and Steer is retired

**Status:** Accepted (2026-10-28); the label is provisional
**Supersedes:** [ADR-137](adr-137-steer-is-the-agent-activity-verb.md) (the name and id only; see Scope)
**Related:** [ADR-163](adr-163-steer-and-log-are-two-projections-of-one-journal.md),
[ADR-210](adr-210-a-feature-flag-is-declared-by-its-extension-and-resolved-default-env-node-account-user.md)
(the verb is a feature flag)

## Context

ADR-137 named the surface that shows every agent and automation, and everything
that needs the user's attention, `Steer`, with the verb id `steer`. The owner
decided on 2026-10-28 that "steer is going away." The surface is not changing;
the word is. The surface that manages all agents and automations, plus anything
needing the user's attention, is now **Control**.

The replacement is provisional. The alternative under consideration is
**Automation**. Because the choice may flip, the decision is as much about
making the swap cheap as about the word.

## Decision

### D1 — The label is Control, held in one constant per repo

The rail label is `Control`. Each repo that renders or documents the label keeps
the display string in exactly one constant, so a later change to `Automation`
(or anything else) is a one-line edit. In appkit the constant is
`CONTROL_LABEL` in `frontend/src/navDefaults.ts`; no other file spells the
label for display.

### D2 — The verb id and the flag key move to `control`

The verb/nav id `steer` becomes `control`; `VERB_STEER` becomes `VERB_CONTROL`.
The module `frontend/src/steer/` becomes `frontend/src/control/` (a `git mv`, so
history follows). The feature-flag key `steer` (ADR-210) becomes `control`, as
does the environment variable: `AXIOM_FLAG_STEER` becomes `AXIOM_FLAG_CONTROL`.

### D3 — Old links, old state, and old flag maps keep working

Nothing that was saved or shared under the old name breaks:

- `#/steer` and `#/decide` both resolve to `#/control`, permanently. The
  `#/decide` alias from ADR-137 D3 is kept and re-pointed.
- `VERB_STEER` and `VERB_DECIDE` remain exported as deprecated aliases of
  `VERB_CONTROL`.
- A flag map served by a node that still says `steer` is read as `control`. The
  appkit flag reader applies that mapping, when `control` is absent from the
  map, and logs a deprecation note. The static fallback `features` config reads
  `steer` the same way.
- A host that maps `NOS_APP_FEATURES` entries onto `AXIOM_FLAG_<KEY>` maps an
  old `steer` entry onto `control`.

The aliases carry no removal date. They are cheap and links are forever; they
are retired by a later ADR, if ever.

## Scope

Superseded: the name `Steer`, the verb id `steer`, and the D3 id decision of
ADR-137. Everything else in ADR-137 stands under the new name: one run stream
with the All, Needs you, Running and Done facets (D1), the declared register
(D2), teaching an agent in words with a read-back (D4), and foreign sessions as
runs (D5). Read "Steer" in ADR-137, ADR-163 and the other historical ADRs and
PRDs as "Control". Those documents are left as written.

## Consequences

**Easier.** One word across the rail, the URL, the flag and the docs, and a
one-line change if the owner picks `Automation`.

**Harder.** For as long as the aliases live, two older spellings resolve, and
tests assert that they do. Historical documents still say Steer and a reader has
to map them.

**Open.** Whether `Control` or `Automation` is the permanent label. If it
changes, the label constant changes; the id `control` stays, since the id is a
route and a flag key and their cost to move is a tenant's saved state.
