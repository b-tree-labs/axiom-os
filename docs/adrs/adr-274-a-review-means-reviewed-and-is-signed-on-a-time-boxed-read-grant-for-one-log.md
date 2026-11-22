# ADR-274: A review means `reviewed`, and is signed on a time-boxed read grant for one log

**Status:** Accepted (2026-11-22). Proposed 2026-11-21. The meaning was decided by the owner on
2026-11-12 (#1817: add `reviewed` by ADR rather than reuse `verified`); the owner accepted the grant
shape on 2026-11-22. Built in #2242 and #2244; ships in the release after 0.70.45.
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) (the closed
meaning vocabulary; rule 8), [ADR-205](adr-205-a-key-belongs-to-a-person-and-what-it-reads-is-resolved-from-grants-and-sharing-programs.md)
(the grant `(principal, role, where, expires_at)`), ADR-250 D4 (the `grant` noun),
ADR-251 D8 and D9 (one check at every door), [ADR-262](adr-262-an-imported-entry-means-imported-and-is-never-a-signature.md)
(a log role held for one log with an expiry), `docs/specs/spec-attestation.md` ("Continuous
review"), #1817.

## Context

A site that is reviewed from outside (an inspector, an auditor, a peer) should be reviewed
continuously rather than in a pre-inspection scramble. The reviewer needs to read one log for a
stated time and nothing else, sign that they reviewed a range of it, and the site needs to see
coverage: when the last review was and what has never been reviewed.

Two things were missing. The closed vocabulary has no meaning for a review. `verified` says that a
person checked a value or a condition, and the spec had borrowed it; a reader filtering for
verifications would then mix them with reviews of whole ranges of records. And a read grant was a
gate key with no issuer and no expiry: the ADR-205 grant store was never built, so access could not
end by itself.

## Decision

1. **One meaning is added to the vocabulary: `reviewed`.** A record with it is a statement in a
   reviews log (any log that declares a type carrying it) that names the log it reviewed in its
   `logbook` field and, optionally, the range examined in `from_seq` and `to_seq`. A review with no
   range covers every record of the log signed before it.
2. **A reviewer's access is a log role, `reviewer`, held for one log at one site with an expiry and
   its issuer.** It is ADR-205's `(principal, role, where, expires_at)` with `where` one log at one
   site, granted like ADR-262's `importer`
   (`axi log role grant @p:site reviewer --log L --until DATE`), refused without `--log` or
   `--until`, and never implied by an administration role, a signing role or a gate pair.
3. **The one check reads the grant on every read.** While the grant is live its holder reads L
   through every door. When it expires or is revoked, every door answers exactly as it does for a
   principal who never held one. A person whose only standing at the site is reviewer grants reads
   the logs those grants name and nothing else, including logs any other sign-in at the site may
   read.
4. **A review is signed only on a live grant for the log it names.** The type's role is
   `reviewer`. The signing check grants that role from the role table, never from the caller, and
   only when the grant covers the named log at the record's site. The grant confers no signing role
   in the reviewed log.
5. **The grant is a `read` grant in the `grant` noun.** `axi grant list` shows it with its expiry
   and issuer, `axi grant revoke` ends it (at the CLI only), and KEEP's expiry sweep reads it. A
   gate key may carry `expires_at` and `issuer` too: after the expiry it resolves as no key does.
6. **Coverage counts only what the reader may read.** `GET /api/v1/attest/<L>/reviews/coverage`
   is a door. It counts a review only when the caller may also read the reviews log that holds it,
   so a reader of L who may not read the reviews sees none and no count of what is hidden.

## Options considered

- **Reuse `verified` (the spec's first wording).** Rejected by the owner on 2026-11-12: it mixes a
  range review with a checked value for every reader that filters by meaning.
- **A gate key per reviewer, with an expiry.** A key is a service credential (posture `service`)
  and cannot sign, and the attest read doors resolve a person's session, not a key. The expiry is
  added to keys as well (point 5), but the reviewer's grant is held by the person.
- **Build ADR-205's grant store and sharing programs first.** That is a much larger change. The
  log-role row already has the shape and an audit of every grant and revoke.

## Consequences

- A reviewer signs in at the site's gate as a person of that site. What a reviewer reads is
  decided by the grant, not by the site's open logs.
- The vocabulary has seventeen meanings. Any reader that enumerates them needs no change, because
  the vocabulary was closed and is now one larger.
- A review that names a range outside the log's records covers nothing. A retracted review still
  counts until a later unit reads retractions into coverage.
- Prompting a reviewer when coverage falls behind (the PRD's notification half) is not built here.
