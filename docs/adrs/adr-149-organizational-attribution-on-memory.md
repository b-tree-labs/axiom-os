# ADR-149: An organization may retract what was learned through it, and nothing else

**Status:** Draft
**Related:** [ADR-026](adr-026-ownership-model.md) (ownership: master +
delegations, four rights), [ADR-027](adr-027-federated-memory.md)
(`axiom://`, cohort registry, multi-signature),
[ADR-033](adr-033-layered-memory-architecture.md) (layered memory),
[ADR-035](adr-035-human-principal-binding.md) (accountability binding),
[spec-federation-policy.md](../specs/spec-federation-policy.md) (visibility
and classification gate outflow),
[spec-classification-boundary.md](../specs/spec-classification-boundary.md),
[spec-aeos-1.0.md](../specs/spec-aeos-1.0.md).

## Context

Memory is scoped per principal. A principal keeps what they learned, because
ownership is the base case in `access.is_visible` and is checked before the
access graph. For a person that is right: losing a login does not un-learn
what you know.

It is also, stated that baldly, something no enterprise will accept. A
consultant works inside an organization for six weeks with an agent at their
side, the agent accumulates memory through that organization's systems, the
engagement ends, and the memory goes with them. Every organization that would
otherwise permit agentic work inside its boundary has a reason to refuse,
and "trust the principal" is not an answer anyone can put in a contract.

The naive fix is worse than the problem. If an organization can retract *a
principal's memory*, then a temporary association becomes a claim over
everything that principal knows, including what they brought with them and
what they learned elsewhere during the same period. That is not defensible
and should not be built.

The distinction that makes it tractable is **what was learned through the
organization** versus **what the principal knows**. An employer can require
the return of its documents and the surrender of its access without claiming
the contents of anyone's head. The same line exists here, and the platform
currently has no way to draw it: nothing on a fragment records under whose
auspices it was learned.

Four dimensions are already recorded and none of them answers this:

| Dimension | Question it answers | Field |
|---|---|---|
| Provenance | who wrote it, when, through which agents and resources | `provenance` |
| Ownership | who may control it | `ownership` (ADR-026) |
| Classification | what regulation constrains it | `classification` |
| Visibility | how far its writer intends it to flow | `visibility` |

Provenance is closest, and it is not sufficient: `resources` records which
systems were touched, not which organization's auspices the principal was
acting under, and a resource identifier is not an accountable party that can
sign a retraction.

## This problem is solved elsewhere, four times

It is worth saying plainly that none of this is novel. Four domains have
faced the same question — what may an institution reclaim from a person who
worked inside it — and all four converged on the same answer.

**Derivative classification** (E.O. 13526 and its implementing directive,
32 CFR Part 2001) is the closest match, almost word for word. It is defined
as *"incorporating, paraphrasing, restating, or generating in new form
information that is already classified, and marking the newly developed
material consistent with the markings of the source information."* That
sentence describes reading ten fragments and writing one conclusion. Two
features matter here: the person doing it **need not hold original
classification authority** — the marking travels with the material, not with
the author's standing — and the rule is **mandatory and automatic**, so
nobody has to establish what anyone intended.

**Portion marking**, from the same directive, requires each portion of a
document to carry its own marking, so a document's level is the maximum of
its portions and a lower portion can be lifted out cleanly. That is the
scaling answer, and it is why fragment-level attribution is the right
granularity rather than an implementation detail.

**Imputed disqualification** (ABA Model Rule 1.10) does the same in legal
ethics: a lateral lawyer's conflicts are imputed to their new firm
automatically, and the remedy since the 2009 amendments is a structural
screen — timely screening, no share of the fee, written notice — rather than
an inquiry into what anyone knew or meant.

**Wall-crossing** in financial services is the fourth, and it contributes
something the others do not. A control group records who was crossed and in
respect of which issuer, adds them to a restricted list, and **brings them
back when the information becomes public or the transaction ends**. Nobody
is asked to forget. Their *action* is restricted, reversibly, and the record
is retained.

## Decision

**A fragment records the organization under whose auspices it was learned,
inside the signed envelope, and that organization may retract fragments
attributed to it. It may retract nothing else.**

Four parts.

**1. Attribution is derived, never declared.** It is computed at write time
from evidence the platform already holds: the organization that
authenticated the session, and the resources the write touched. A principal
cannot set it, an extension cannot pass it, and a caller cannot suppress it.
A field a writer can fill in is a field an interested writer will fill in
wrongly, and the whole value here is that the tag is *traceable* rather than
asserted.

**2. Attribution lives inside the signed provenance.** It is part of the
canonical bytes the fragment signature covers, following the precedent
`provenance.origin` set: the key is omitted entirely when absent, so
fragments written before this ADR keep verifying. Stripping or editing an
attribution invalidates the signature, and the federation gateway already
verifies signatures on projection. That is what makes the tag
tamper-evident rather than merely present.

**3. Retraction is a narrow named right, not ownership.** An organization
does not become an owner, a master, or a delegate. It holds exactly one
power: to retract fragments carrying its attribution. This is deliberately
*not* modelled as a `Right` in ADR-026's ownership enum, because those
rights are delegated **by** the owner, and this one is not the owner's to
give — it is a precondition of the association, agreed before the work
starts.

**4. Withdrawal has two outcomes, and the default is the reversible one.**

*Quarantine* is the default: the fragment becomes unreadable and unrecallable
by anyone, and is retained under seal for audit. This is wall-crossing, and
it is the better instrument for almost every real case. The organization gets
what it needs, which is that the principal cannot use the material. The
principal gets something they may actively want: **proof** of compliance, and
proof is what buys admission to a restricted domain in the first place. And
it is reversible, so an association that resumes does not start from nothing.

*Tombstone* is the irreversible outcome, for when an organization requires
destruction rather than restriction. It uses the mechanism deletion already
has, signed by the retracting organization, and tombstones already propagate
across federation, so a peer drops its copy on verifying the signature and
the attribution match.

Both reconcile with federated memory by construction rather than by a second
path. An organization states which it requires as part of the association
agreement, so the principal knows before the work starts.

### What an organization may retract

Only fragments whose attribution names it. Concretely, and stated as
limits because the limits are the defensible part:

- **not** fragments the principal wrote before the association began
- **not** fragments written during it under a different organization's
  auspices
- **not** fragments with no attribution at all, which is the default and
  which covers a principal working on their own account
- **not** the principal's identity, ownership, or any fragment's lineage
  record — a tombstone records that something was retracted

### Derived fragments: attribution propagates, and intent is not consulted

A fragment synthesised from others inherits its parents' attribution through
`provenance_parent`. **Attribution is a set, and any named organization may
retract.**

The justification is derivative classification, not suspicion. An earlier
draft of this ADR argued the rule as anti-evasion — that without inheritance
somebody could launder material by summarising it — and that framing was
wrong, because it makes the rule contingent on a claim about intent that
nobody can establish and everybody can contest. The classification world
faced exactly this and deliberately built the rule the other way: *any*
incorporation, paraphrase, restatement, or generation in new form carries the
source's marking, whoever did it and for whatever reason. It is a statement
about lineage, which is observable, rather than about motive, which is not.

That is also what makes it scale. A rule requiring an intent determination
requires a person; a rule over lineage is a graph traversal.

A fragment drawing on two organizations therefore carries both attributions
and is reachable by either. The principal's protection is not that mixing
launders a fragment — it is that an **unattributed** fragment is untouchable
by anyone, and that this protection is robust precisely because attribution
is derived from evidence rather than asserted by an interested party.

The rejected alternative is sole-attribution-or-none, which is the first
reading of "solely attributable" and is kinder to the principal. It is
recorded rather than left unsaid, and the reason it loses is not that it
invites bad behaviour but that it makes an organization's reach depend on
whether a second source happened to be present, which is arbitrary.

## Options considered

**Organization as an ownership master or delegate (ADR-026).** Reuses
existing machinery. Rejected because the rights in that enum flow from the
owner, and this power does not: it is agreed as a precondition of
association, not granted by the principal afterwards. Modelling it as a
delegation would also make it revocable by the principal, which defeats it.

**Retract by resource, as `memory.lifecycle.forget_resource` does.** Already
built, and it is the right mechanism for a *service* principal losing access
to a system. Rejected as the general answer because a resource is not an
accountable party. A resource identifier cannot sign a tombstone, cannot be
held to an agreement, and does not survive an organization renaming its
systems. The two compose: both produce tombstones, and this ADR does not
replace that path.

**A separate organizational memory store.** Partition writes made under an
organization into a store it controls. Rejected because it breaks the
principal's memory into fragments that cannot be composed or recalled
together, which is the thing memory is for; and because it answers the wrong
question — the issue is not where a fragment sits but who may retract it.

**Deletion as the only outcome.** What the first draft proposed. Rejected
after looking at wall-crossing: irreversible destruction is the wrong default
for a temporary association, it gives the principal nothing they would want,
and it is the outcome least able to keep its promise across federation. It
remains available for organizations that require it.

**An unsigned tag.** Simplest. Rejected because Ben's precondition was
hacker-resistance and an unsigned tag is a suggestion: the party with the
most motive to remove it is the party holding the fragment.

## Consequences

**Committed to:**

- A signed, derived attribution on every fragment written under an
  organization's auspices, omitted when there is none.
- An organization-scoped retraction that produces signed tombstones and
  propagates across federation on the existing path.
- AEOS conformance language: an extension MUST NOT write, alter or suppress
  an attribution, and MUST propagate it through derivation. This is what
  makes the guarantee portable to extensions we did not write, which is the
  reason it belongs in the standard rather than only in this implementation.

**Gets easier:**

- An organization can state a memory-retraction policy as a precondition of
  agentic work inside its boundary, and the platform can honour it.
- A principal can accept that precondition without surrendering what they
  know, because the scope is visible, narrow and enforced by the same
  signature the rest of the fragment relies on.
- A federated peer needs no new protocol: it verifies a signature and drops
  a copy, which it already does.

**Gets harder:**

- Attribution must be derived at write time, so the write path needs to know
  the authenticated organizational context. Where that context is absent the
  honest answer is no attribution, which means an unattributed fragment is
  not evidence of a principal acting privately.
- Inheritance through derivation has to be enforced on every path that
  composes fragments. That is a real ongoing obligation, and it is the one
  place a gap silently widens the further it goes unnoticed, since an
  unattributed derived fragment looks exactly like a legitimate one.
- Retraction cost grows with the size of the attributed set. The machine
  learning literature on the same problem converged on sharding — SISA
  (Bourtoule et al., IEEE S&P 2021) partitions training data so unlearning
  retrains only affected shards, and remains the reference approach. If
  attribution-scoped retraction becomes hot, the equivalent move is to shard
  the store by attribution so that a retraction touches a bounded subset
  rather than scanning the whole of a principal's memory.
- **A retraction is a demand, not a guarantee of erasure.** A peer that has
  gone dark cannot be made to drop its copy; a peer that has been compromised
  may lie about having done so. What the platform can honestly offer is that
  the demand was issued, signed, propagated and recorded, and that every
  peer still participating has acknowledged it. GDPR practice is candid about
  the same limit — a controller must inform its processors and cannot
  guarantee their behaviour — and this ADR is candid about it too, because an
  enterprise that is promised erasure and later discovers it means
  best-effort will not extend trust a second time.

  This is a further argument for quarantine as the default. Quarantine is
  enforced where the fragment is read, so it holds on every node still
  running the platform, whereas deletion depends on every holder complying.

**Explicitly not decided here:**

- Whether a principal may see *what* was retracted. There is an argument for
  a receipt naming the count and the organization without the content.
- The identity model for an organization. This ADR assumes an organization
  is an existing signing party in the federation trust graph, and says
  nothing about how one is admitted.
- Retention of the attribution itself after withdrawal. The tombstone
  carries it; whether that record is itself subject to policy is a question
  for the retention work, not this one.
- Who may lift a quarantine, and on what evidence. Wall-crossing brings a
  person back when the transaction ends, which implies the organization
  releases it; whether a principal may petition, and whether an expiry can be
  agreed up front, is left open.

## References

- Executive Order 13526, *Classified National Security Information*, and its
  implementing directive 32 CFR Part 2001 — derivative classification and
  portion marking.
  <https://www.archives.gov/isoo/policy-documents/cnsi-eo.html>
- ISOO, *Marking Classified National Security Information*.
  <https://www.archives.gov/files/isoo/training/marking-booklet-revision.pdf>
- ABA Model Rule 1.10, *Imputation of Conflicts of Interest*; screening for
  lateral lawyers added by the 2009 amendments to Rules 1.10 and 1.0.
  <https://www.americanbar.org/groups/professional_responsibility/publications/model_rules_of_professional_conduct/rule_1_10_imputation_of_conflicts_of_interest_general_rule/>
- SEC OCIE, *Staff Summary Report on Examinations of Information Barriers* —
  control groups, watch and restricted lists.
  <https://www.sec.gov/about/offices/ocie/informationbarriers.pdf>
- Bourtoule et al., *Machine Unlearning*, IEEE S&P 2021 — SISA.
  <https://arxiv.org/pdf/1912.03817>
