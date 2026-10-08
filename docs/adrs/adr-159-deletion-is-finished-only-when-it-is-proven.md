# ADR-159: Deletion is finished only when it is proven

**Status:** Draft (2026-10-05)
**Related:** [ADR-158](adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md) (regimes), [prd-regulated-data-regimes](../prds/prd-regulated-data-regimes.md), [ADR-026](adr-026-ownership-model.md) (ownership), [ADR-027](adr-027-federated-memory.md) (provenance is immutable), [ADR-033](adr-033-layered-memory-architecture.md) and [ADR-149](adr-149-organizational-attribution-on-memory.md) (tombstones), [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md), [ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md) and [ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md) (signed records), [ADR-157](adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md)

## Context

Regulated content has to be removable, and the person who is accountable for it has to be able to be told that it is gone. Four obligations make that concrete. A GDPR erasure request must be acted on without undue delay and within one month, extendable by two (Articles 12(3) and 17), and the controller must pass the erasure on to recipients (Article 19) and be able to demonstrate what it did (Article 5(2)). HIPAA requires disposal of media and keeps the documentation of what was done for six years (45 CFR 164.310(d), 164.316(b)(2)). Export-control records are kept five years (15 CFR 762.6; 22 CFR 122.5). NIST SP 800-88 Rev. 1 describes a certificate of sanitization recording the media, the method, the tool and its version, the operator, the verifier, the date and the disposition. A data owner may also simply want to know that something they put in is gone.

Axiom does not meet this today. It has logical retraction: a memory fragment can be tombstoned and the tombstone propagates (ADR-026, ADR-033, ADR-149). It has no way to find every copy of a piece of content, delete them, and show that none remain. The AEOS specification lists the right to be forgotten as an open question. A survey of one running deployment on 2026-10-05 found the shape of the problem: the same text can sit in an indexed document table with its embeddings and name index, in an in-memory answer cache, in raw request traces kept in plain text, in the memory ledger, in bulk-ingested files, in exports, in a chat front end's own history store, and in database dumps. Any single-store delete leaves the others, and nobody finds out.

Deleting is also at odds with an Axiom invariant. Provenance is immutable (ADR-027): a record's `(T, U, A, R)` tuple is fixed at write time. A purge cannot rewrite history, so it has to remove the content and keep a trace that something was removed.

## Decision

**A purge is a workflow that ends in a signed receipt, and a purge without a receipt did not happen.**

1. **Every store of content registers a purge provider.** Database tables and their indexes, vector indexes, name and search indexes, caches, files and ingest artifacts, request traces, the memory ledger, exports, extension caches, and backups each register an object with four operations: `find(selector)`, `delete(plan)`, `verify_absent(selector)` and `residual()`, which says what it could not delete now and when that ends. A store with no provider is *unregistered*. A purge reports every unregistered store and refuses to claim completeness while any exist.
2. **Select, plan, approve, execute, verify, receipt.**
   - *Select*: by content hash, source identifier, label, pattern set, time window, or data subject reference.
   - *Plan*: a dry run that reports counts per store. Derived copies (embeddings, summaries, cached answers, trace lines) are found through lineage keys carried from the source (its hash and its identifier), not by hoping text matches.
   - *Approve*: by a policy that is configuration, not code: none, one approver, or two. The requester can be the data owner or the data subject.
   - *Execute*: idempotent, resumable and ordered, derived copies before sources, and failing closed. A step that cannot complete stops the purge and reports.
   - *Verify*: independent of the deletion, by a different code path and identity. It searches by hash, re-runs the regime's detectors over the store, and, for embeddings, queries for nearest neighbours of the removed content.
   - *Receipt*: below.
3. **The receipt is signed, content-free and kept in the attestation logbook** (ADR-142, ADR-150). It follows the NIST certificate's fields: the stores, the method, the tool and version, the operator, the verifier, the date and the final disposition. It adds a hash of the selector (never its content), counts before and after per store, the verification method and result, the unregistered stores, the residual copies with their expiry dates, the approvals, the request identifier, the regime and legal basis, and the result of the hold check. It never contains regulated content, and it identifies a data subject only by a salted reference.
4. **Backups are stated honestly.** A deployment chooses per regime among three policies: *crypto-shredding* (content is encrypted under per-regime or per-subject keys, and destroying the key makes every backup copy unreadable), *expiry* (the backup is deleted on a schedule, and the receipt names the date), or *re-scrub on restore* (a tombstone list is applied before restored data is served). The receipt says which applies. A receipt may not say content is deleted everywhere while a readable copy remains in a backup.
5. **A purge keeps a tombstone.** The content is destroyed, and an append-only tombstone remains holding an identifier, a time and the receipt reference. That keeps provenance chains verifiable (ADR-027, ADR-149) without keeping the content.
6. **A hold blocks a purge.** A legal hold or a retention duty stops a purge until it is released. The attempt, the refusal and the obligation that caused it are recorded, and the requester is told which obligation applies. This is how a GDPR erasure request meets a HIPAA retention duty without either being silently dropped.
7. **Receipt retention is configurable per regime.** The shipped default is **six years**, the longest specific period found among the regimes (HIPAA six years from creation or last effective date; export-control five; sanitization records commonly at least three; GDPR has no fixed period and relies on being able to demonstrate compliance). A deployment can set a different period and cannot set none. Receipts are kept minimal so that holding them is not itself a privacy cost.
8. **Purges are triggered, not only requested.** Triggers are a change to a classification list or profile, the end of a retention period, a leak detected at ingest, a request from an owner or a data subject, and an instruction from a regulator or counsel. Scheduled verification sweeps re-run detectors over stores to catch content that returns, for instance when a source is re-ingested.
9. **A purge propagates.** Where content was shared along federation edges, the purge issues signed tombstones to each recipient node (the mechanism ADR-026 and ADR-149 already define), the receipt lists each peer's acknowledgement or its absence, and an unacknowledged peer is a residual copy, stated as such. This is also the shape of the GDPR Article 19 duty to inform recipients.
10. **Purge is machine-speed by design.** Selection and deletion are index-backed operations with a stated time budget in the plan; a purge that must scan petabytes to find one subject's rows is a design failure of the store's lineage keys, and the plan surfaces it.
11. **The machinery is tested by trying to make it fail.** Synthetic canary items carrying regulated markers are planted in every registered store and purged on a schedule. The purge battery fails if any canary survives, and it includes an unregistered-store case that must be reported.
12. **Requests carry their clock.** A request records its due date under the regime (one month for a GDPR erasure, extendable by two with a reason given within the first month), and an overdue request raises an alert.

## Options considered

**Delete and trust.** The default today. It leaves copies and gives no evidence, which is what the obligations above rule out.

**One central delete script.** It works the day it is written and forgets every store added afterwards. The provider registry makes an unregistered store a visible fact and not a silent omission.

**Rely on encryption alone.** Crypto-shredding is the right answer for backups and is adopted there. For live stores it needs per-subject keys everywhere, which is a larger change than the problem needs, and it still needs a verification step.

**Receipts that list what was deleted.** They leak the regulated content they are meant to prove is gone. A hash of the selector and counts give the proof without the content.

**Rewrite history to remove the record.** It breaks the immutable provenance that signed chains depend on. A tombstone does the job.

## Consequences

Every place that stores content must be inventoried and given a provider. That includes places that were never treated as data stores, notably request traces and caches, and it gives each store an owner. An unregistered store is a standing finding.

Verification is a real cost: detector sweeps and vector queries over large stores take time, and the plan must say how long a purge will take so the regime's clock is met.

The receipt becomes a durable record with its own retention, access control and signature, which ties this to the attestation logbook and its operations.

A purge is a high-consequence action. The approval policy, the hold check and the dry-run plan are what keep it from being a data-loss tool, and they ship with it.

Follow-up work, sequenced in the PRD: the provider interface and registry; providers for the built-in stores; the plan and verify steps; the receipt format in the logbook; the backup policies; hold records; the canary battery; and the clocks and alerts.
