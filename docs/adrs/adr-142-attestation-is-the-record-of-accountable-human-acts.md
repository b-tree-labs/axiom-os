# ADR-142: Attestation is the platform's record of accountable human acts

**Status:** Accepted (2026-09-30)
**Related:** [ADR-126](adr-126-typed-decision-receipts.md) (typed decision receipts),
[ADR-042](adr-042-chat-driven-corrections-and-correction-aware-retrieval.md) (corrections),
[ADR-115](adr-115-generic-medallion-answering.md) (declarations),
[ADR-114](adr-114-mcp-authority-enforcement.md) (authority on MCP),
[ADR-135](adr-135-graduated-autonomy.md) (graduated autonomy),
[ADR-137](adr-137-steer-is-the-agent-activity-verb.md) (Steer),
[ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md) (signed chain),
[ADR-144](adr-144-a-person-confirms-exactly-what-is-recorded.md) (confirmation),
[ADR-145](adr-145-voice-stays-on-site-and-a-spoken-answer-signs.md) (voice),
[ADR-146](adr-146-signing-from-the-browser.md) (browser signing),
[prd-attestation.md](../prds/prd-attestation.md), [spec-attestation.md](../specs/spec-attestation.md),
[ADR-020](adr-020-federation-identity-and-relationships.md) (node identity keys)

## Context

The platform records what people do in at least four places, each in its own
shape:

- a held action's approval sets `Action.decided_by` / `decided_at`;
- a case decision is a `CaseVerdict`, an ADR-126 typed decision receipt whose
  `decider` may be `human`;
- a steward's correction, retraction or reclassification is a declaration
  (ADR-042, ADR-115);
- a Steer decision is taken in the decide flow (ADR-137).

Tenants are about to add more: a consumer layer needs an operations log for
regulated equipment now, and the same request is coming as shift logs,
maintenance logs and inspection findings. Each would build its own signed, append-only table.

These are one thing: **a person put their name to a statement, with a
meaning, about something, at a time, having been shown something.** They
differ in what the statement is about, not in what makes it trustworthy.
Keeping them separate has already cost precision. `Action.approve()` defaults
`decided_by` to `"@gate:auto"`, so the field that is supposed to answer "which
person approved this" can hold a machine, and nothing distinguishes the two.

The platform also records machine work, in two places that must not be
confused with human acts:

- the **audit trail** records what the system observed any principal doing;
  it is automatic and complete;
- **receipts** and the **action ledger** record what software did and claims.

The forward case sharpens this. The primary System-Two scenario is many small
autonomous units supervised by few accountable people. The scarce resource is
the licensed or accountable person, and the thing that makes one person
answerable for many units auditable is a formal record of what that person
decided, approved, observed and delegated.

## Decision

We introduce **attestation** as a platform primitive: the durable record of an
act by an interactively authenticated human who is accountable for it.

An attestation has:

- a **signer**: a human principal, with a snapshot of their authority at
  signing (roles, qualifications, grants) and the assurance level of the
  authentication;
- a **meaning** from a closed platform vocabulary (`authored`, `observed`,
  `performed`, `verified`, `approved`, `rejected`, `decided`, `corrected`,
  `retracted`, `reclassified`, `acknowledged`, `relieved`, `delegated`,
  `revoked`, `sealed`);
- **subjects**: typed references to what the act concerns;
- **content**: the statement, typed by the book it belongs to;
- **evidence as presented**: what the signer was shown or read back when they
  signed (ADR-144);
- two times, `occurred_at` and `recorded_at`.

Attestations are grouped into **books**, each named by one bus token
(`[a-z0-9_]+`, e.g. `approvals`), so book events are plain subjects
(`attest.<book>.<event>`). A book is a declared adaptation that
supplies entry types, fields, obligations, co-signature rules, seal rules and
export templates. The Ops Log, a shift log, a maintenance log and an
inspection log are books. Approvals, case decisions and steward declarations
become **platform books**. Those subsystems keep their own state tables and
write the human act as an attestation, so there is one answer to "who did
this, with what authority, having seen what".

**Rules that follow:**

1. **Software never attests.** Agents, services and the digital twin may
   create drafts. Only a human principal signs. The API refuses a signature
   from a non-human principal.
2. **An automatic decision is not an attestation.** `@gate:auto` and any rule
   or model decider remain decisions recorded as receipts. They are never
   rendered as "signed by".
3. **An attestation is a receipt class.** It is signed and
   content-addressed (`axiom://attest/<sha256>`); its distinguishing property
   is that its signer is a person. The surface word is plain: "signed by".
4. **Linked, not merged, with machine records.**
   - An attestation that approves, rejects or overrides an agent action
     references the action ledger entry or receipt.
   - The action references the attestation that authorised it.
   - A `delegated` attestation is the record that raised an agent's autonomy
     ceiling (ADR-135); authority reserved to humans is exactly the set of
     acts that must be attestations.
5. **Corrections keep ADR-042's distinctions.** A correction says the
   statement was wrong. A retraction says stop relying on it. A
   reclassification says it was the wrong kind. Each is its own attestation
   referring to the original, which is never edited.
6. **Missed obligations are system facts, not attestations.** "A check was due
   and none was signed" is recorded by the platform. No one signed it.
7. **Attestation never gates physical control.** Books may notify and
   escalate. They never act on equipment or a controlled process, and never lock out
   a person.
8. **Administering the platform grants no authority in a book.**
   - Node administrators, developers and vault custodians can operate a node:
     deploy, back up, monitor and rotate keys.
   - Those roles cannot sign in, alter or remove anything from any book.
   - Each book declares its signing roles. Platform administration roles are
     excluded by default, and a book cannot opt them back in.
   - Nobody can alter or remove a record. Records are append only.

## Options considered

- **Each tenant builds its own logbook** (the status quo trajectory). Lost:
  four divergent signing, correction and export designs; no shared answer for
  auditors; the "@gate:auto" imprecision repeated in each.
- **A generic "Ops Log" in Axiom.** Lost: it names one book and would still
  leave approvals, verdicts and declarations outside. The primitive is the
  signed human act; the log is one adaptation.
- **Extend the audit trail to carry human acts.** Lost: the audit trail is
  complete and automatic by design. Mixing deliberate acts into it destroys
  the property that makes each useful, and the Logging PRD already forbids
  mixing the two.
- **Extend typed decision receipts (ADR-126) to cover all human acts.** Lost:
  many attestations are not decisions (observed, performed, relieved). A
  `CaseVerdict` with a human decider now *references* an attestation rather
  than standing in for one.

## Reuse, not re-implementation

The primitive is assembled from what the platform already has. It adds only
the record, the signing step and the book declaration:

| Need | Reused |
|---|---|
| Who signed and how strongly | Principal postures (`axiom.infra.principal`), `step_up`, webgate OIDC, federation node identity (ADR-020) |
| Roles and qualifications | Role bundles, OpenFGA site relations, site scope from the credential |
| Deciding and confirming | The R21 decision anatomy and R20 densities; Steer's decide card; approval bridge and interactive channels |
| Due, done, missed | Schedule consumer seam (`register_slot`, `record_actual`, `register_cadence`) |
| Telling someone | Notifications (`alert`, recipients); the oversight brief via `receipts.sources.register_source` |
| Watching integrity | Hygiene `node_health` findings on the heartbeat |
| Events and live views | The event bus (NATS-shape subjects) and one SSE helper (ADR-147) |
| Quantities | `axiom.uncertainty` wire form |
| Files, PDFs, archives | `axiom.infra.storage`, publishing PDF providers, backup |
| Verbs | Skill functions per ADR-056, behind the capability chokepoint; the sign verb never declares `mcp` or `agent_tool` surfaces, as with approve/reject today |
| Speech | One speech module extracted from the signals voice extractor's Whisper use |
| Analytics | Data platform CDC to bronze, projected to gold, like other extensions' event logs |

## Consequences

- New builtin extension `attest` (schema, API, SDK `axiom.attest`), plus a
  manifest `provides` kind `book` so an extension can declare a book.
- Approval gate, case verdicts and steward declarations migrate to write
  their human act through `axiom.attest`. Until migrated they remain as they
  are, and the spec lists each gap.
- `Action.approve()` stops defaulting `decided_by` to a machine. A machine
  decision is recorded as one, explicitly.
- The first consumer builds its operations log as a book, in its own repository.
  The Logging PRD's §3 human-only rule becomes a consequence of this ADR rather
  than local policy.
- Calibration (ADR-126 D3) gains human-observed outcomes as a first-class
  witness source.
- Surfaces gain one signing experience: confirm (ADR-144), sign (ADR-146),
  read back by voice where enabled (ADR-145).
- Terminology ledger: "signed by" and "read back" are plain; "attestation"
  and "book" are internal-only, and on surfaces a book shows its own name
  ("Ops Log").
