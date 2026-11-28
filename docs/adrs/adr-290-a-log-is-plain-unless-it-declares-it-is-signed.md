# ADR-290: A log is plain unless it declares it is signed

**Status:** Accepted (2026-11-25)
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) (attestation),
[ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md) (signed chain),
[ADR-144](adr-144-a-person-confirms-exactly-what-is-recorded.md) (confirmation),
[ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md) (the logbook),
[ADR-251](adr-251-indexes-and-context-are-labelled-projections-the-record-is-sealed-never-compacted-one-check-at-every-door.md)
(one record, labelled projections),
[ADR-262](adr-262-an-imported-entry-means-imported-and-is-never-a-signature.md) (imported history),
[prd-attestation.md](../prds/prd-attestation.md)

## Context

Every log the platform declared until now was a formal record. Each entry is drafted, shown to a
named person with a role, answered, and chained with an evidence block (ADR-142 to ADR-144). That
is the right shape for the few logs a business process audits later: a regulated operations log, an
approval, a change of authority. It is the wrong default for everything else. A shift note, a work
diary or a service journal needs a name, a time and a place to be searched, not a ceremony.

The platform had no way to say so. A log declaration has no key for it, so every log a site declared
was signed, and a site that wanted a plain log either built its own table or put up with the
ceremony. The platform's wording had already moved the same way: "sign" is kept for formal log
entries, and everything else is an action that is recorded under the person's name.

The cost of a signature is not in the cryptography. The chain is sealed by the node key either way,
and that stays. The cost is the draft, the presentation, the readback, the role pairing and the
confirmation that a signature means something. A plain log should not pay for them.

## Decision

**D1. A log is plain unless its declaration says `signed = true`.** `[logbook].signed` is a boolean.
Absent, the log is plain. Signed is the special case, declared by the log that needs it.

**D2. A declaration never loses a signature by saying nothing.** A log that does not state `signed`
but uses a feature only a signature can satisfy is signed: a confirmation policy, an observed
field, a fresh sign-in, presence, a signing-device list, a co-signature, an interval, an obligation,
or a crosscheck. `signed = false` beside such a feature is refused when the log loads,
naming the feature. So a regulated log written before this decision keeps signing without a
change, and a log cannot be made plain by accident or by contradiction.

**D3. A plain log is written in one step.** `service.record_entry` writes one entry for a person who
is signed in with a signing posture (`attested` or `sso`, as for signing; a service or open session
is refused) at or above the log type's posture, and holds a role the type allows. There is no draft,
presentation or readback. The record names the person (`signer.kind = "session"`), carries the
roles that allowed it, and is chained and sealed by the node exactly like a signed one. It has no
evidence block, because nothing was presented. A signed log refuses `record_entry`, and a plain
log refuses `create_draft` and everything built on it (co-signature, seal, supplement, tombstone):
those are signing features.

**D4. A signed log does not quietly become plain.** If the newest entry of a log was signed by a
person (`signer.kind = "human"`) and the log is now declared plain, a one-step write is refused,
saying so. The log's owner declares `signed = true` again or starts a new log. Plain to signed needs
no check: earlier entries stay what they were, and each record says how it was written.

A plain log cannot carry the meanings `imported` (only an import record has it) or `reviewed` (it
needs a live reviewer grant, which only a signed log checks). Like the signed path, a plain write does
not check who may *read* the log: reading is checked at every read door, and a log whose `readers` are
narrower than its type's roles should say so in its own declaration.

**D5. Plain and signed entries share the chain.** They use the same table, chain, head, indexes,
doors, labels, paging, search, export, cold tier, archiving and retention. Verification checks the
chain and the node seal; a plain entry has no person's signature to check and is not reported as
missing one. The seal, archive and tombstone of a period are acts on the log (D8), the same for both.

**D5a. An entry has a size limit.** A plain entry (title, note and fields as JSON) may hold at most
64 KiB, and its content must be representable in the chain (no floats; a decimal is a string). The
chain is never compacted, and a plain log takes entries more casually than a signed one.

**D8. Archival acts are signed by the accountable actor, and are not a reason to sign entries.**
Sealing a period, archiving it and tombstoning it are acts on a log, not entries in it, and a log of
either kind may perform them under its declared retention rule. The act is signed by the **accountable
human where one can be traced**, and by the acting identity where none can:

1. a human acting signs for themselves, through their session;
2. an agent acting is signed for by the human `axiom.agents.activation.accountable_for` names for it
   (the site's configuration, the activation record, the agent's declaration, or the site default);
3. a machine (the scheduler, a node) is signed for by the human the site names as accountable for it
   (the site default, `*`), if any;
4. if no human is traced, the agent or machine signs as itself.

The record names the signer, the actor who performed the act (`performed_by`), and where the trace came
from (`accountable_source`), and says the act was signed *as the accountable person* rather than by their
own hand. **Declared accountability is the authority.** Systems rarely declare authority separately, so
the platform does not ask for it: a human name tied to the actor is what it can ask for, and it is
enough for any act the declared rule allows (`live_days`, `delete_after_days` within the platform's
retention floor, which nothing shortens). More of the chain, such as who the human answers to, is
recorded when known and never required. An act outside a declared rule is not made by a machine or an
agent for anyone: it needs the human present.

A seal and an archiving or deleting rule are therefore not signing features: `signed = false` beside them
is accepted, and the log stays plain. A declaration that says nothing about signing and declares one keeps
what it meant before this decision, which was signed, so no existing log silently loses its signatures;
a new plain log that archives says `signed = false`. `service.record_seal` and `service.record_tombstone` are the acts;
both work on a plain log and a signed one. A person acting for themselves is held to the roles the log
names (`[seal].by_roles`, `retention.tombstone_by`); an agent or machine acting for an accountable
human is held to the rule alone. A log that became plain only because it said nothing is reported when
it loads, so an owner who meant it to be signed sees that.

**D6. The platform's own logs say `signed = true`.** The approvals log and the settings authority
log declare it, as do the guide's example logs.

**D7. Edit is a correcting entry, appended.** A plain entry is never rewritten. A person edits it by
appending a supplement (`service.record_supplement`) that names the original (`target`,
`target_digest`) and why (`reason`: correction, clarification or addition), sealed by the node like any
entry, with the same refusals as a signed log's supplement. The person must hold a role the original's
type lists. The original stays as it was and the entry's thread shows both. Acknowledgements,
co-signatures and seals stay signed-log features.

## Consequences

- A site declares a plain log in a few lines: an id, types, roles. It gets search, paging, export,
  retention, archive and access control with no other work. The write API is
  `POST /api/v1/attest/<log>/entries`.
- A plain entry proves who wrote it and when, and that the chain was not altered after the node
  sealed it. It does not prove that the person was shown exactly what was recorded. A log that
  needs that proof declares `signed = true`.
- Anything built on drafts and a person's signature (acknowledgements, co-signatures, seals,
  tombstones, intervals, obligations) is for signed logs. A plain log edits an entry by appending a
  correction (D7). If a plain log later needs one of the signed features, it becomes a signed log
  from that point (D4 allows that direction).
- A plain log is archived and deleted by the acts of D8 under its declared rule, like a signed one.
  Committing and erasing an archived period still check a person's role in the site's role table; applying
  the accountable-actor rule to those two steps, and a scheduled driver for routine archiving, follow.
- A plain entry's `occurred_at` may be any past time the person states, with no evidence behind it;
  `recorded_at` is the server's. A log that must prove when something happened is a signed log.
- Declarations in other repositories that rely on the old behaviour keep signing through D2 when
  they use any signing feature. A declaration that uses none and wants a signature must now say
  `signed = true`; `axi log validate` states which a declaration is, and why.
- The Log page shows signed and plain entries together. Marking each entry's kind is a view
  concern and is not decided here.
