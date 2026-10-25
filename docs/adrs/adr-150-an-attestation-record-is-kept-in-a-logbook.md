# ADR-150: An attestation record is kept in a logbook

**Status:** Accepted (2026-10-01)
**Amends the vocabulary of:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md),
[ADR-144](adr-144-a-person-confirms-exactly-what-is-recorded.md),
[ADR-145](adr-145-voice-stays-on-site-and-a-spoken-answer-signs.md),
[ADR-146](adr-146-signing-from-the-browser.md),
[ADR-147](adr-147-surfaces-receive-live-updates-over-sse.md)

## Context

ADR-142..147 call attestation's unit a **book**: a declared, append-only
record of signed entries, such as an operations log or a shift log. The
platform also has a **Book**, the library: the artifact repository that a
person reaches from the navigation rail, where uploaded files are kept and
classified. A consumer layer extends it with its own classification scheme.

Two different things under one word would collide in the rail, in the
`/api/v1` routes, in the extension manifest's `kind`, in the CLI, and in
every signed record, where the word is a field name. The attestation
vocabulary had not shipped in a release, so the record format could still
change without migrating anyone's signed history.

## Decision

1. Attestation's unit is a **logbook**. "Book" belongs to the library alone.
2. The rename applies everywhere attestation names its unit:
   - the declaration file's table (`[logbook]`);
   - the manifest (`[[extension.provides]] kind = "logbook"`, schema
     `ProvidedLogbook`);
   - the API (`/api/v1/attest/logbooks`, `/api/v1/attest/{logbook}/…`);
   - the CLI (`axi attest logbook list|validate`);
   - the signed record field (`logbook`) and anchor heads;
   - the database columns (migration `0008`).
3. ADR-142..147 keep their text, as accepted ADRs do. Read "book" in them as
   "logbook". Their decisions are unchanged.

## Consequences

- A record is `{"logbook": …}` from the first release. Before this, only test
  and development schemas held records, and they hold no signed history
  anyone relies on.
- Bus subjects keep their shape (`attest.<logbook id>.signed`); only the
  word for the segment changes.
- The surfaces planned as `BookView` are `LogbookView`.
- The library's Book is free to take `kind = "book"` in the manifest schema
  when it is built.
