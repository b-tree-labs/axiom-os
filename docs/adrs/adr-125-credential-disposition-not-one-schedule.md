# ADR-125: Credentials declare a disposition; one rotation schedule does not fit them

**Status:** Proposed
**Date:** 2026-09-21

## Context

The expiry audit asked one question of every credential — when does this
expire — and gave one piece of advice when the answer was missing: record an
expiry. Six credentials had no expiry, so six findings repeated on every run,
and for most of them the advice was **wrong**:

- A PyPI token has no expiry to record. A date written there is a lie that fires
  a false alarm on an arbitrary day.
- A service account on a partner's network is not ours to rotate at all;
  suggesting it invites breaking a system we do not run.
- An Entra client secret genuinely does expire, and only a person with console
  access can learn the date.

An unclearable finding is worse than no finding. It repeats until people filter
the report, and then the one real warning lands in a channel nobody reads. That
is how a dead credential survived six months here.

Tightening the rotation cadence would not have helped. The failure was not a
too-long interval; it was a credential with no date **and no declared
disposition**. A shorter schedule on something we cannot rotate produces a
faster-repeating unclearable finding.

## Decision

**A credential declares what can be done about it, and the audit asks the
question that answer makes answerable.**

| Disposition | Meaning | The question |
|---|---|---|
| `self_rotatable` | We hold the authority and an API exists | Rotate at `ROTATE_AT_FRACTION` (2/3) of lifetime — **never at expiry** |
| `human_rotatable` | Expires, console-only | Read the date from the console and record it |
| `externally_owned` | Somebody else controls it | Who is the owner, and when do we review? |
| `non_expiring` | No expiry exists | When do we review? |

An unknown disposition is **refused**, not stored: a typo that silently
persisted would let somebody believe they had classified a credential when they
had not, and the finding would vanish without the question being answered.
`externally_owned` without an owner is refused for the same reason.

**Rotation never triggers on expiry.** A self-rotating credential authenticates
as itself, so once expired it can no longer rotate — which is precisely the wall
this episode hit (HTTP 401 on the rotation call). Two thirds of the lifetime
leaves a third of the window for a rotation to fail, be noticed and be retried
while the credential still works.

**Proof of life replaces expiry for everything we cannot date.** We cannot
predict death for a credential we do not control; we can notice it within one
heartbeat instead of six months. `vault reconcile --apply` stamps
`last_verified_at` when a credential demonstrably answers, and that going stale
(>90 days) is its own finding. A future review date says when to look again — it
does not say the credential still works, and those are different claims.

A failed or unreachable check stamps **nothing**. A dead credential that looked
healthier after a check than before it would be the worst possible outcome.

## Consequences

**Good.** On this install the change turned five unclearable findings into two
actionable ones within minutes: three credentials were declared
non-expiring/externally-owned with owners and review dates, one became "read the
date from the Entra console", and one remains deliberately undeclared because
nobody yet knows whether that vendor's keys expire — which is now a question the
report asks rather than a silence.

**Cost.** A vocabulary is a commitment; adding a fifth disposition later means
revisiting every classification. Four were chosen because they partition on the
two axes that actually change the advice: *can we rotate it* and *does it
expire*.

**`vault declare` reports what is still outstanding** after a declaration, and
does not claim success while the next heartbeat repeats the same finding.

**Accepted limitation.** Nothing here can tell whether a vendor's credential
expires. That remains a human lookup, and the report now names it as such
instead of demanding a date that may not exist.
