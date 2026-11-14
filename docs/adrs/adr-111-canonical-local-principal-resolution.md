# ADR-111 — One local principal, one spelling: canonical resolution at the write boundary

**Status:** Proposed — 2026-09-12
**Owner:** @ben
**Related:** [prd-identity-and-bindings](../prds/prd-identity-and-bindings.md)
§2 hole 4, §5.6, §5.7 (this ADR implements them), ADR-035 (human principal
binding — mandates `accountable_human_id`, does not say who resolves it),
ADR-020 (`@name:context` naming), ADR-026 (ownership transfer),
[prd-identity-acquisition](../prds/prd-identity-acquisition.md) (install-time
owner acquisition).
Supersedes nothing.

## Context

A real install carried **11,170 memory fragments under one identity and 105
more stranded across five other spellings of the same person**: a work address,
a personal address, a git commit address, a machine-derived handle, and one
malformed value welding a `@name:context` prefix onto an email. A sixth
partition came from branding rather than identity — a second physical ledger
under a consumer-branded state directory, holding writes the primary ledger's
readers never saw.

Nothing reported it. It surfaced months later because a query returned empty
and someone went looking by hand.

**The cause is that the substrate has no single answer to "who is writing".**
Five independent resolvers each carry their own fallback chain:

| resolver | location |
|---|---|
| `resolve_principal` | `axiom/infra/principal.py` |
| `resolve_principal_id` | `axiom/memory/session_capture.py` |
| `current_principal_id` | `axiom/memory/session.py` |
| `_resolve_principal` | `axiom/extensions/builtins/mcp/platform_primitives.py` |
| `resolve_principal` | `axiom/extensions/builtins/connect/identity_link.py` |

None is wrong in isolation. Together they mean the spelling recorded depends on
which code path performed the write, and no persona or vendor account is
involved — this is the substrate disagreeing with itself.

### Why this is worse than a rejected write

A rejected write is visible; the user fixes it and moves on. A write filed
under a spelling nobody queries is **present, attributable, correctly signed,
and unfindable**. Recall degrades with no error, no warning, and no symptom
except an answer quietly thinner than it should be — which is indistinguishable
from the model simply not knowing something.

### Why the existing safeguard did not fire

`check_axiom_memory_principal_reconciliation` was built for exactly this failure
and sampled the **newest 25 fragments**. The stranded writes spanned five
months, so the recent sample was clean and the check passed on every run. It
was the right instrument for drift *starting* and blind to drift that had
already *happened*. (Widened in the same change set; recorded here because a
future safeguard designed the same way will fail the same way.)

## Decision

### 1. One resolver

A single `resolve_local_principal()` becomes the only function permitted to
answer "who is writing". The five existing resolvers become callers of it or
are deleted.

The requirement is not that the answer be clever. It is that there be **exactly
one answer**, because the failure mode here is disagreement rather than error —
five correct-looking functions produced six identities.

### 2. Validation at the write boundary

A principal must match one of the two declared conventions — `name@domain` or
`@name:context` — and nothing else. Malformed values are rejected at write time
rather than stored and discovered later.

`@user@example.org` is not a judgement call. It is both conventions at once, and
no resolver intended to produce it; it minted an identity nobody chose.

### 3. Normalization, narrowly

Only unambiguous repair: a stray `@` before an address (the address form takes
no prefix; the handle form takes no second `@`), and case folding for addresses,
which are case-insensitive by specification.

Handles are left untouched. Reinterpreting `@name:context` is a different
decision belonging to whoever owns that convention, and a normalizer that
guesses at it will eventually merge two agents.

### 4. Aliases are declared, never inferred

A human may legitimately write under a git commit address, a machine handle, or
a personal address. **The substrate cannot know those are one person.**

Inference is rejected because its failure is unrecoverable: merging two people
who are genuinely distinct destroys a boundary that cannot be reconstructed from
the merged ledger, whereas leaving two spellings unmerged is a nuisance that
stays fixable. The asymmetry decides it.

Aliases therefore live in the persona config (PRD §5.3) as an explicit
`aliases = [...]` list. Resolution folds a *declared* alias to the persona's
principal at write time. An undeclared spelling is reported by drift detection
and never silently absorbed.

### 5. Drift is reported, not prevented

Two valid spellings of one person are indistinguishable from two people sharing
a node. A check that treated the second case as an error would be wrong for
every multi-user install.

`axi identity drift` therefore reports the dominant identity, the minority
spellings, how many fragments are stranded under each, and any malformed values.
The number that makes someone act is **how many memories cannot be found and by
whom** — not a boolean.

Known non-human owners (the system actor, test fixtures) are excluded. A warning
that can never clear is one people learn to scroll past, which is the same
failure as a gate that can never pass being a gate that is always bypassed.

### 6. One ledger per install

A consumer's brand must not fork the memory store. The state directory is a
platform property; branding selecting a different one produced a second ledger
by accident, and its writes were invisible to every reader of the first.

A persona wanting genuine physical separation gets it through the mechanism in
PRD open question 5, deliberately and on the record.

### 7. Migration re-signs; it does not edit

The condition exists in the field, so the design owes a repair path, and two
properties are non-negotiable — both learned performing this migration by hand:

- **Re-sign, do not edit.** The canonical signing payload covers the whole
  fragment except the signature slot, `ownership` included. Rewriting the owner
  field alone invalidates every migrated fragment. Migration re-signs with the
  same node key: the attester is unchanged, only the owner spelling moves.
- **Back up, dry-run, then verify the whole ledger** — not only the migrated
  rows. A migration that repairs 105 fragments and corrupts one is a net loss,
  and a full verification pass is the only evidence that has not happened.

## Consequences

**A write can now fail that previously succeeded.** A malformed principal is
rejected rather than stored. This is the point, and it is a behaviour change:
any caller constructing principals by string concatenation will surface.

**Aliases require configuration.** Someone whose git address differs from their
work address must declare it, or their writes land under a second identity and
appear in drift output. This is deliberate — the alternative is the substrate
guessing at human identity, which fails unrecoverably.

**Five call sites change.** Each existing resolver's fallback chain must be
examined before it becomes a caller: any behaviour only that resolver provided
is either folded into the canonical one or consciously dropped. This is the bulk
of the work and the part most likely to surface surprises.

**Drift reporting will be noisy on existing installs** until they migrate. That
is accurate rather than unfortunate — the ledgers really are partitioned, and a
check that hid it to stay quiet would be the original bug in a new place.

**One ledger is now a constraint on branding.** A consumer product cannot choose
its own memory state directory. Consumers that want separation must ask for it
as a persona decision rather than obtain it as a side effect.

## Alternatives considered

**Infer aliases from an edit-distance or domain heuristic.** Rejected on the
asymmetry in §4: a false merge is unrecoverable, a missed merge is a nuisance.
No heuristic clears that bar for identity.

**Reject on drift rather than report.** Rejected because two spellings are
indistinguishable from two people, so this breaks every multi-user install to
fix a single-user problem.

**Keep the resolvers and add a linter.** Rejected: the resolvers do not disagree
statically, they disagree at runtime depending on which path executes. A linter
cannot see that, which is precisely why five reviewed, tested functions produced
six identities.

**Leave the second ledger and merge at read time.** Rejected as strictly more
machinery for a worse guarantee — every reader would need the union logic, and
any reader that forgot it would silently see half the data, which is the bug.
