# ADR-271: A reader of part of a site never sees the journal's node-wide number

**Status:** Accepted (2026-11-21)
**Related:** ADR-251 (one check at every door), ADR-241 (a record lives with its writer), journal
J1 to J3 (#1772), issue #2224

## Context

The journal gives every row it stores one number, `journal_events.seq`, from a single identity
shared by every lane and every site on the node. The Timeline, its live stream (event ids and
`Last-Event-ID`), the Tail, the drawer and the list's `next_cursor` all carry that number. Audit
rows also carry the audit chain's number (`ref.seq`, `source_id`, `audit://actions/<n>`), which is
node-wide as well.

A person who may read only some logs at a site sees those numbers jump (#4488, then #4490). The
jumps tell them roughly how many rows they could not see and when those rows were written. No
content, log name or signer leaks, but the count and the timing do.

A signed record's own `seq` does not have this problem. The chain is kept per (site, log), so a
reader of a log sees every number in it, without gaps. The first wording of #2224 named the record
`seq`. The code shows the leaking number is the journal's.

## Decision

1. **Whole-site readers see today's responses, byte for byte.** A whole-site reader is a caller
   who, at every site in their scope, may read every declared log, decided by the one LD-1 check
   (`journal.attestations`). An unbounded operator on a node with no site bound counts as one.
   When GUARD cannot decide, the answer takes the partial form. A position the client sends back
   is still read, so a stream that loses GUARD ends as it did before (LB-J3-A18).
2. **Every other caller never receives the journal's number, or an audit chain number, from a
   journal door.** On the list, the stream, the Tail (`journal.tail`), the journal read skill and
   the drawer, each row has `seq` replaced by `key`, an opaque token. An audit row has its chain
   number removed from `ref`, `source_id` and `link`.
3. **Cursors and stream event ids are opaque and checked by the server.** A token is a keyed
   pseudo-random permutation of the number plus a tag, under a node key. The same row always gets
   the same token, so a page can deduplicate rows. A token that does not decode is refused (400),
   and so is a raw number from a partial reader. Resume (`cursor`, `Last-Event-ID`) still works.
4. **A row shows a number only where the number is the reader's to see.** An attestation row
   carries `number = {n, logbook, label, text}`, where `n` is the record's per-log `seq` and
   `text` reads "#12 in Shift log". An audit row, and any other row, carries no number.
5. **The chain does not change.** Verification, anchors, tombstones and the record doors keep the
   record `seq`, which is already per log. The journal keeps its `seq` internally for ordering,
   paging and resume.

## Consequences

- No migration is needed. The per-log number is already stored as the record's `seq`.
- A page tells which form it got by whether rows carry `key`. A whole-site reader's rows are
  unchanged, so pages written before this decision keep working for them.
- A whole-site reader can still see gaps caused by lanes or classification ceilings they may not
  read. The decision is scoped to logs, as Ben decided on 2026-11-21. Closing the lane case would
  mean widening "whole-site" to every lane and ceiling. That is a separate decision.
- The node key is a random value created on first use in the node's state directory, readable
  only by its owner. If it is replaced, old cursors stop decoding and the client starts again
  from the newest rows.
