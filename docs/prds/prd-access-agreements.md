# PRD: Access agreements — who has agreed to what, and what that unlocks

**Product / Feature:** Access agreements (export-control authorization, NDAs and data-use agreements as one primitive)

**Owner:** Ben Booth   •   **Status:** Draft   •   **Last updated:** 2026-10-02

**Related:** [spec-classification-boundary](../specs/spec-classification-boundary.md) (this extends §2.1's `proprietary` stamp and §2.2's principal attestations), [ADR-142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md) / [143](../adrs/adr-143-attestations-are-signed-into-a-verifiable-chain.md) / [144](../adrs/adr-144-a-person-confirms-exactly-what-is-recorded.md) / [146](../adrs/adr-146-signing-from-the-browser.md) / [150](../adrs/adr-150-an-attestation-record-is-kept-in-a-logbook.md) (attestation), [ADR-114](../adrs/adr-114-mcp-authority-enforcement.md) (authority on MCP), ADR-151 (roles; in review), [ADR-152](../adrs/adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md) (the decision this PRD builds on), [prd-attestation](prd-attestation.md)

---

## 1) Elevator pitch

Some content may only be seen by people who have agreed to something: an export-control
authorization, a vendor's NDA, a data-use agreement. Axiom should treat all three as one
primitive. Content says which agreement it requires; a person who has signed it, recorded as
an attestation, may see it; anyone else is told exactly what to sign, and can sign it
in the product. An administrator sets this up and grants access without being able to read
the material.

## 2) Problem / Opportunity

- **Export control is a per-client flag, not a per-person fact.** A client is "EC-capable" or
  not (`AXIOM_MCP_CLIENT_EC_CAPABLE`). There is no way to say that this person is authorized,
  no record of who authorized them or when, and no expiry.
- **The control list is a file.** The terms that mark content as controlled live in
  `export_control_terms.txt`, with an allowlist beside it. Nothing in the product lets an
  administrator list, add, remove or audit entries, and a node whose runtime configuration
  directory is not where the files are read from silently has no user list at all.
- **A model's guess has been the reason people were refused.** Measured 2026-10-02 against a
  local 3.8B model: the default classifier flagged **22 of 32 public questions** (public history,
  textbook science, a published report) as export controlled while missing none of 12 requests
  for controlled technical data. It also withheld a capabilities listing that contains no data
  at all. This is fixed in the short term by [ADR-152](../adrs/adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md);
  the root cause, that nothing deterministic says who may see what, remains.
- **A vendor wants to use Axiom.** Their drawings and other proprietary material must be
  visible only to people who have signed an NDA. The vendor should supply its own legal text.
  A guest being onboarded, or an existing user who simply lacks the privilege, should go
  through the same signing path rather than a manual process by email.
- **Nothing records who agreed to what.** Without a record there is no expiry, no revocation
  and nothing to audit.

## 3) Principles (the invariants every phase must keep)

1. **Deterministic.** No model output grants or denies access (spec-classification-boundary §4,
   invariant 1). A model may advise the administrator who maintains the rules; it is never the
   reason a person is refused.
2. **Three independent checks, all of which must pass.** *What you may do* is a role and scope
   (ADR-151). *What you have agreed to* is an attestation you hold. *Where the result may go* is
   whether the destination, for example a model running outside the enclave, may receive it.
   Holding an agreement is never expressed as a role (that way lies `ec-viewer`, `nda-viewer`,
   `ec-nda-creator`, and no way to say "this person's NDA expired"), and no role implies a
   destination capability. A person's authorization does not release controlled content to a
   destination that may not receive it.
3. **An agreement is signed by a person and recorded as an attestation.** Software never signs
   (ADR-142). The record is chained and verifiable (ADR-143). The agreement text is
   content-addressed so that what was signed is provable, and the person is shown exactly that
   text when they sign (ADR-144).
4. **The requirement is a label on the content.** The checker takes whatever granularity the
   label is attached to, a corpus or a single document, so per-document labels stay possible
   without a migration.
5. **The administrator governs and reads nothing by default.** The role that adds people to an
   agreement holds `agreements:govern` (ADR-151's scope grammar) and no read scope.
6. **Three refusals that mean three different things.** "You may not", "you have not signed that"
   and "that cannot be sent where you are asking from" are distinct sentences with distinct
   remedies, never one generic denial.
7. **Fail closed.** A missing label, an expired or revoked attestation, or an error withholds.
   Content without a stamp is treated as the lowest trusted level until stamped (spec §4,
   invariant 2).

## 4) Goals & Success Metrics

- **Primary goal:** every denial is explainable and fixable by a person, and no denial is a guess.
- Zero results withheld by a model verdict alone (the model verdict is logged for review).
- 100% of denials name which of the three checks failed and what would fix it.
- 100% of grants record signer, date, the hash of the agreement text, expiry and revocation state.
- A control-list change takes effect without a restart and is attributed to a person.
- A vendor guest goes from invitation to access in under one business day of administrator time.

## 5) Key Users / Personas

- **Agreements administrator.** Defines agreements, adds and removes people, reads who signed
  what. Holds `agreements:govern`. Reads no protected material by default.
- **Content owner** (a vendor administrator, or a creator). Uploads their agreement, labels
  their content, sees who has signed, revokes.
- **Invitee.** A guest, or an existing user lacking the privilege. Needs to see exactly what
  they are agreeing to and sign it without a side process.
- **Compliance officer.** Audits who could see what, when, and under which agreement.
- **Node operator.** Chooses the node's defaults and which destinations are in the enclave.

## 6) Scope: capabilities by phase

| Phase | Capability | Acceptance (one line) |
|---|---|---|
| **P0** (shipped 2026-10-02) | The client-sink gate honors stored labels and the control list; the model verdict is advisory | A public-labelled result is released; a restricted label or a control-list term withholds; the model's guess is logged, not acted on |
| **P1** | Control-list administration | An authorized administrator can list, add and remove control terms and allowlist entries, each change attributed and audited; the advisory log ("what the model thought") is reviewable; a node with no configured list location says so |
| **P2** | Agreement definition | An administrator defines an agreement: name, kind (export-control authorization, NDA, data-use), the legal text (uploaded, hashed), counterparty, term and expiry, whether a new version requires re-signing; content labels reference it |
| **P3** | Signing | An invitee is shown exactly the text and signs; the attestation is recorded; an optional counter-signature by the counterparty's authorized signer; states are requested, signed, active, expiring, expired, revoked |
| **P4** | Enforcement | Every retrieval surface checks that the person holds a valid, unexpired, unrevoked attestation covering the content's label, and that the destination may receive it; the three refusals are distinct; every access is logged with the agreement it relied on |
| **P5** | Vendor self-service | A company's administrator uploads its own agreement, labels its content, sees signers and revokes, without Axiom staff |
| **P6** | Federation policy | Each agreement kind declares whether a peer's attestation is accepted; the default is none, and export-control authorization is never accepted from a peer |

## 7) Non-goals

- Legal advice, or validating the legal text. The vendor owns its document.
- Deciding which e-signature standard satisfies a given jurisdiction (open question 5).
- Certifying a deployment for any export or classification regime; accreditation is
  per-deployment (spec-classification-boundary §1).
- Handling classified material beyond what that spec already defines.
- Inferring anyone's nationality or authorization. Both are asserted by an attestation from an
  authority, never guessed from a name or an address.

## 8) Non-Functional / Constraints

- **Security:** deterministic gates; append-only, tamper-evident audit; no protected content in
  logs; an administrator who governs agreements cannot read the material they unlock.
- **Performance:** the agreement check adds one attestation lookup per distinct label in a
  result, cached for the session and invalidated on revocation.
- **Operations:** list and agreement changes take effect without a restart.
- **Substrate:** domain-agnostic; nothing here names a consumer's field.

## 9) Open decisions for the owner

1. **May the agreements administrator read the material they grant access to?** Proposed: no.
   It is cheap to widen later and awkward to narrow.
2. **Does export-control authorization ever federate?** Proposed: never. The reason is
   jurisdictional, different from the generic "peer claims are refused by default"
   (ADR-151 §4), and should be stated as its own rule.
3. **Label granularity.** Proposed: the label lives on the content, and the checker honors
   whichever granularity it is attached to.
4. **Who may add people to the export-control list on a node?** A designated compliance role
   distinct from the node owner, or the owner?
5. **E-signature standard for NDAs and counterparty acceptance.**
6. **Re-signing.** When an agreement's text changes, do earlier attestations lapse? Proposed: yes.

## 10) Evidence behind this document

- 2026-10-02, local 3.8B model, 32 public and 12 controlled-data prompts: old default prompt
  flagged 22/32 public, 0/12 controlled missed; a prompt that defines export control flagged
  1/32, 0/12 missed. Longer variants (acronym rule, explanation rule, worked examples) were
  worse (14, 11, 17 of 32).
- 2026-10-01, on a live node through the MCP, as a non-EC-capable client: a numeric series was
  withheld while a neighbouring series for the same seconds was released; a capabilities
  listing with no data in it was withheld as export controlled.
- Before and after through the real gate and model, same eight payloads: three public payloads
  withheld on the previous code pass now; a restricted label and two control-list matches are
  withheld both before and after.

## 11) Acceptance & rollout

P0 ships with ADR-152. P1 is the next increment and needs only the roles from ADR-151.
P2 to P4 are one design and one release train, built on the attestation primitive; P5 and P6
follow. Each phase lands with its own tests and a docs update, per the repo's phase rule.
