# ADR-262: An imported entry carries the meaning `imported`, and an import is never a signature

**Status:** Accepted (2026-11-13, by the owner)
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) (rules 1, 5, 6 and 8),
[ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md) (the chain),
[ADR-146](adr-146-signing-from-the-browser.md) (the gate mints what a browser acts with),
`docs/working/design-note-import-records.md` (Axiom #1804), `docs/specs/spec-attestation.md`
(Migration; the record table), #1802 (the label "Imported, not signed").

## Context

A log's history often starts somewhere else: a paper log typed up, an older logbook
application. One chain should hold the whole record and verify end to end, so that history is
placed on the chain as records of kind `import`, which the spec's record table already names.

Two things in the closed vocabulary do not fit. Every record carries a meaning, and the fifteen
meanings each say what a person did here: `authored`, `observed`, `performed`, and so on. An
imported entry was authored or observed by someone, somewhere, but this node witnessed none of
it. Saying `authored` claims more than the chain knows; saying nothing breaks the rule that
every record has a meaning, and every reader that filters by one.

The second is who may write one. An import is not a signature, so no signing role is the right
gate, and administration holds no authority in a log (ADR-142 rule 8).

## Decision

1. **One meaning is added to the vocabulary: `imported`.** It is usable only by a record of
   kind `import`, and refused on every other kind; a log cannot declare it for an entry type.
   A record of kind `import` carries no other meaning. The meaning the source itself stated,
   when it stated one, is kept as text in `content.original_meaning`, never as the record's
   meaning.
2. **An import record names no signer.** `signer` is `{kind: "unverified", display: <the
   source's own name or initials, as text>, principal: null}`, never `human` and never a
   resolved principal. The person who ran the import is `imported_by` on every record, and
   the action audit chain holds one entry per committed batch. Every surface says "Imported,
   not signed" (#1802) and shows no signature badge. An import never meets an obligation.
3. **Importing is a log-scoped role, `importer`, granted with an expiry.** It is granted like a
   signing role, for one log (`axi log role grant @p:site importer --log L --until DATE`), and
   a grant without `--until` is refused with the reason. No administration role and no signing
   role implies it.
4. **A person commits; software prepares.** A dry run (counts, every mapping decision, the
   input's digest, warnings) may be asked for by anyone who may read the log, software and
   agents included. The commit names the dry run's digest and is made by a person holding the
   role: at the command line (`axi log L import FILE --commit DIGEST`, asked at the prompt), or
   from the web through the browser hand-off at the node's gate that signing uses (ADR-146,
   #1936). There is no raw HTTP commit, and no MCP tool or agent tool commits.
5. **A whole batch can be withdrawn, never deleted.** A holder of the `importer` role withdraws
   a batch with a required reason by appending one record of kind `import_withdrawal`, meaning
   `retracted`, naming the batch and every record in it. Nothing is changed or removed. A batch
   with a correction on any of its entries cannot be withdrawn. A withdrawn batch's source
   references no longer count as on the chain, so the history can be imported again as a new
   batch. A single imported entry is corrected the existing way: a correction or supplement by
   a person who holds a role in the log (ADR-142 rule 5).

## Options considered

- **`meaning = null` for an import.** Breaks the rule that every record has a meaning and every
  reader that filters by one.
- **Reuse `authored`.** Claims that a person authored the entry on this chain, which nobody did.
- **Reuse an administration role to import.** Refused by ADR-142 rule 8: an import writes to
  the log.
- **A grant from the grant noun (#1740) instead of a log role.** Possible later; a log role with
  a required expiry is what the owner chose, because it reads and is granted like the roles a log
  already has.
- **A raw HTTP commit, or one from MCP.** An agent or a key could then write a log's history
  without a person at a sign-in. Refused.
- **Delete or rewrite a mistaken batch.** Records are append only (ADR-142 rule 8). A withdrawal
  record says the batch is not to be relied on, and the batch stays readable.

## Consequences

- `MEANINGS` gains `imported`; the declaration check refuses it on an entry type; the record
  kinds gain `import_withdrawal`. Both are permanent.
- Verification does not change: the chain and the standalone verifier check digests and node
  signatures, never a signer's kind.
- Idempotence is by `(site, log, source_ref)` over live, non-withdrawn imports. Because records
  are append only, withdrawal cannot be expressed by a predicate on `attest_records`; the unique
  partial index lives on a small reference table (`attest_import_refs`, one expand-only
  migration), whose rows a withdrawal marks withdrawn.
- Imports may be committed at any time, after live entries too. They are appended in the order
  committed; views and exports sort by `occurred_at`; each imported entry shows its batch and
  original time; the dry run warns for an entry dated after the log's first live entry.
- Intervals are never opened, closed or renumbered by an import; a live number continues above
  the highest imported number.
