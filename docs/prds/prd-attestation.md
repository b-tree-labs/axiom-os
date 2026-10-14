# PRD: Attestation — the record of accountable human acts

**Owner:** Axiom platform • **Status:** Draft • **Last updated:** 2026-09-29
**Spec:** [spec-attestation.md](../specs/spec-attestation.md)
**ADRs:** [142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md) (the primitive),
[143](../adrs/adr-143-attestations-are-signed-into-a-verifiable-chain.md) (signed chain),
[144](../adrs/adr-144-a-person-confirms-exactly-what-is-recorded.md) (confirmation),
[145](../adrs/adr-145-voice-stays-on-site-and-a-spoken-answer-signs.md) (voice),
[146](../adrs/adr-146-signing-from-the-browser.md) (browser signing),
[147](../adrs/adr-147-surfaces-receive-live-updates-over-sse.md) (live updates), [150](../adrs/adr-150-an-attestation-record-is-kept-in-a-logbook.md) (logbook, not book)
**First tenant logbook:** a consumer layer's operations log (specified in that consumer's repository)

---

## 1. Elevator pitch

When a person puts their name to something — "I performed the equipment check",
"I approve this agent's change", "this reading was wrong, here is the right
one", "I relieve the watch" — the platform records it once, the same way
everywhere: who, with what authority, meaning what, about what, having been
shown what, and verifiably unchanged since.

Logs (operations, shift, maintenance, inspection), approvals, case decisions
and data corrections are all adaptations of that one record.

## 2. Problem and opportunity

- **Human acts are scattered.** Approvals set `decided_by` on an action. Case
  decisions are typed decision receipts. Steward corrections are declarations.
  Each has its own shape, and one of them, `Action.approve()`, defaults its
  decider to a machine (`@gate:auto`) in the field meant to name a person.
- **Every regulated tenant is about to build a logbook.** The first consumer
  needs an operations log for regulated equipment now. Shift, maintenance and inspection logs follow,
  as do agronomy field records. Without a primitive, each builds its own
  signed append-only table, correction model, export and verifier.
- **Signatures prove clicks, not agreement.** No surface records what the
  person was shown when they approved. A misrendered value or a misheard word
  is undetectable afterwards.
- **Hands-busy people cannot use screen-only confirmation.** Console operators
  and technicians at equipment need to speak an entry, hear it read back, and
  confirm by voice. High-reliability operations already require exactly this repeat-back
  discipline of people.
- **The forward case.** Autonomous units will outnumber the people accountable
  for them. What makes one accountable person answerable for many units is a
  formal record of what that person decided, approved, observed and delegated.
  That record is also the independent witness that System-Two calibration
  needs.

## 3. Definitions

| Record | What it says | Written by | Example |
|---|---|---|---|
| **Audit trail** | What the system observed any principal doing | The platform, automatically, completely | "principal X invoked `schedule.create`" |
| **Receipt / action ledger** | What software did and what it claims | Software | "backup completed; 132 tables restored in validation" |
| **Attestation** | What an accountable person asserts, performed, decided or approved | A person, deliberately | "I performed the 14:30 equipment check; pressure 4.2 bar" |

A **logbook** is a declared adaptation of attestation for one purpose. It sets the
entry types and fields, who may sign what, co-signature and seal rules, time
obligations, and export templates. Surfaces show a logbook by its own name; the
words "attestation" and "logbook" do not appear on tenant surfaces.

## 4. Goals and success metrics

- **One answer to "who did this, with what authority, having seen what"** for
  every human act on the platform.
  - Metric: 100% of approvals, case decisions and steward declarations write
    an attestation (after migration).
  - Metric: 0 machine decisions rendered as "signed by".
- **A new logbook is configuration plus a thin extension, not a new subsystem.**
  - Metric: the first consumer logbook's attestation code is confined to its
    logbook declaration, domain rules and views.
  - Metric: a second logbook (shift or maintenance) ships in under two weeks.
- **Independently verifiable.**
  - Metric: an evidence package verifies with a stdlib-only script, no secret
    and no platform access.
- **Hands-free where needed.**
  - Metric: a spoken equipment check from wake phrase to signed record under
    30 s median.
  - Metric: 0 records created without a confirm response.

## 5. Users

| Persona | Needs |
|---|---|
| **Signer** (operator, technician, reviewer, approver, steward) | Sign quickly and correctly; correct without erasing; speak when hands are busy |
| **Co-signer / witness** | See exactly what they are countersigning |
| **Accountable supervisor** | See what is due, what was missed, what is waiting for them |
| **Auditor / regulator** | Search, export, verify independently; see corrections and gaps |
| **Logbook designer** (extension builder) | Declare a logbook without re-implementing signing, chains, correction, export or confirmation |
| **Agents** | Draft for a person; link their actions to the attestation that authorised them; never sign |

## 6. Capabilities

1. **Attestation record.** Signer (human principal only), authority snapshot,
   assurance level, meaning (closed vocabulary), subjects, content, evidence
   as presented, `occurred_at` / `recorded_at`, source (screen, voice, CLI,
   paper re-entry, import).
   - *Acceptance:* a non-human principal's signature is refused.
2. **Signed, verifiable chain.** Canonical form, content address, node
   Ed25519 signature, optional personal signature, one gapless chain per
   (site, logbook), anchors, append-only guard.
   - *Acceptance:* tampering by a database superuser is detected by the
     standalone verifier.
3. **Corrections without erasure.** Supplement / correction, retraction and
   reclassification as distinct attestations referring to the original.
   Co-signature and acknowledgement are also attestations referring to it.
   - *Acceptance:* the original is byte-identical after any number of
     corrections.
4. **Logbooks.** Declared by an extension (manifest `provides` kind `logbook` plus a
   logbook file). A logbook declares:
   - entry types and typed fields, with units required for quantities;
   - meanings allowed per type;
   - roles and qualifications per type and meaning;
   - co-signature, required assurance level, and intervals (what opens and
     closes a run, shift or work order);
   - obligations, seal rules, the confirmation policy, the voice grammar,
     export templates and retention.
   Sites override within bounds the logbook allows.
   - *Acceptance:* an operations log, a shift log and a maintenance log are
     each expressible as a logbook.
5. **Obligations.** "An entry of type T is due every N minutes while condition
   C holds." Missed obligations are system facts, with notification and
   escalation hooks. There is never an action on equipment.
   - *Acceptance:* a restarted service back-fills missed obligations, with the
     detection delay recorded.
   - **Required vs optional.** A logbook marks each obligation required or
     optional. A missed required obligation raises an alarm; a missed
     optional one raises a notice.
6. **Drafts.** Private, mutable, auto-saved. They are created by people or,
   with `origin` recorded, by agents, integrations and speech. A draft is
   never part of the record.
7. **Confirmation, pluggable, for anything.**
   - Any proposal (a draft, an agent action, a case resolution, a
     declaration) is presented through one or more modalities: screen,
     spoken read-back, interactive notification, kiosk, CLI.
   - The person answers in the platform's one decision anatomy: Sign,
     Hold (keep as draft) or Ask (correct or question).
   - What was presented and the answer become evidence on the attestation.
   - Presentation is rendered from the exact content being signed.
   - *Acceptance:* a signature is refused unless it names the digest that was
     presented.
8. **Voice capture.**
   - The agent's name is the wake word, and speech follows the CLI's
     noun-verb grammar with the agent asking for each next level; this and
     push-to-talk start recording; an end phrase or silence ends it.
   - Detection happens on the device, and transcription and speech output on
     the site node. No audio leaves the site.
   - Speech produces a draft, which goes through confirmation.
   - Audio is retained per logbook policy, with its checksum on the record.
   - *Acceptance:* no network request carries audio to any host other than
     the site node.
9. **Signing assurance, on the platform's postures.** A logbook sets, per
   meaning:
   - a posture floor: `sso` (IdP sign-in) or `attested` (a personal key);
   - optionally a freshness window (sign in again within N minutes);
   - optionally a personal-key signature;
   - optionally a witness (a co-signature by a second person).
   `open` and `service` principals never sign. Browser signing uses a one-use
   grant bound to the presented digest.
10. **Harness integration.**
    - Approvals, case verdicts and steward declarations write their human act
      as attestations, in platform logbooks.
    - Agent actions and attestations link both ways.
    - Raising an agent's autonomy ceiling is a `delegated` attestation;
      lowering it is `revoked`.
    - Human-observed outcomes are attestations of meaning `observed` that
      calibration can use.
11. **Search, export, evidence packages.**
    - Filter by logbook, time, interval (run, shift, work order), signer,
      meaning, subject and text.
    - Export formats: PDF (logbook template), deterministic plain text, JSON
      Lines and CSV.
    - An evidence package adds checksums, public keys, anchors and a
      standalone verifier, and is registered as a receipt.
12. **Observed fields.** A logbook may mark a field as one the person must
    observe and enter themselves. No surface offers a value for it before
    signing. Machine sources may cross-check it after signing, and a
    disagreement is recorded beside the signed value, never replacing it.
    - *Acceptance:* no presenter or surface renders a machine-supplied value
      for an observed field.
13. **Inverted audit: continuous review.**
    - Reviewers (e.g. an external inspector) hold standing,
      policy-time-boxed access.
    - Their reviews are signed attestations over a range of records.
    - The site sees review coverage and the last-review date, so it can
      prompt the reviewer instead of preparing for an inspection day.
    - *Acceptance:* coverage for any period is answerable in one query.
14. **Separation of duties.** Administering the platform (node admin,
    developer, vault custodian) grants no authority to sign in, alter or
    remove anything from a logbook. Logbooks declare their signing roles, and
    administration roles are excluded.
    - *Acceptance:* a node administrator's sign attempt is refused in every
      logbook.
15. **Partitioned chains, event-triggered obligations, machine-sourced
    fields.**
    - A logbook may partition its chain by an entity (e.g. one chain per field)
      instead of per site.
    - Obligations can fire after an event ("within 7 days of completion"),
      not only on a cadence.
    - Values from equipment, labs or imports are attached by checksum and
      labelled by source, never presented as a person's observation.
16. **Shared-device signing.** Devices are enrolled with a class: personal
    session, kiosk, tablet or phone. A kiosk is claimed by a badge tap and
    signed with a PIN or verified voice; it locks when idle and signs out
    after each act. Phones acknowledge by default and sign only where a logbook
    allows it.
17. **Migrating an existing chain.** A consumer's pre-existing hash chain
    imports as unverified records, keeping the old hash as provenance and
    recording the one-time verification result.
18. **Surfaces (appkit):**
    - logbook view (stream plus facets), entry form (schema-driven; observed
      fields empty), confirm card, voice capture control;
    - record thread (original plus corrections and signatures), proof badge,
      obligation countdown, live indicator, handoff/seal sheet, print styles.
    - The same parts serve consumer surfaces, Steer's decide flow and receipts.

## 6a. Built from the platform

Attestation adds a record, a signing step and a logbook declaration. Everything
else is reused (ADR-142 has the full table):

- **Identity:** principal postures, step-up, webgate and federation node keys.
- **Roles:** role bundles and site relations.
- **Surfaces:** the R20 densities and R21 decision anatomy, and the Steer
  decision card and stream.
- **Time:** the schedule seam for due, done and missed.
- **Attention:** notifications, plus the oversight brief for misses and
  integrity.
- **Health:** hygiene findings for chain health.
- **Transport:** the event bus and one SSE helper.
- **Values:** `axiom.uncertainty` for quantities.
- **Output:** storage, publishing and backup for exports.
- **Speech:** one speech module shared with signals.
- **Analytics:** data platform CDC.

If a logbook designer needs something not on that list, the gap is fixed in the
platform, not in the logbook.

## 7. Non-goals

- Not an access-audit log (the audit trail already is one) and not an agent
  log (the action ledger and receipts are).
- Not identity proofing or HR records. Qualifications are read from the
  identity directory, not kept here.
- Not a control system. Logbooks never act on equipment or lock people out.
- Not a document-signing product for arbitrary files. Attached files are
  referenced by checksum.

## 8. Non-functional

| Area | Requirement |
|---|---|
| Sign latency | p95 < 1 s from confirm to signed (no freshness window) |
| Live propagation | p95 < 500 ms to every open surface of the logbook at the site (SSE) |
| Verification | Full-chain verification daily; evidence-package verification with stdlib only |
| Availability | Logbooks run on the site node; loss of off-site connectivity does not stop signing |
| Privacy | Audio only on site; retention stated; people told when audio is kept |
| Licensing | All dependencies ≤ Apache-2.0, including wake models and speech models |
| Records | Retention per logbook, overridable per site within regulatory minimums |

## 9. Phases

| Phase | Delivers |
|---|---|
| **A — Record** | `attest` extension; record model, canonical form, chain, node signing via key custody, guard, anchors, verifier; drafts; logbook declaration and validation; CLI `axi attest` (verify, export, show); read-only MCP tools |
| **B — Sign** | Confirmation core with screen and CLI modalities; browser signing grants with re-authentication; appkit confirm card, record thread, proof badge, live stream; the first tenant logbook |
| **C — Harness** | Approval gate, case verdicts and steward declarations write attestations; `@gate:auto` recorded as a machine decision; agent-action links; delegation attestations under ADR-135; observed outcomes into calibration |
| **D — Voice** | Site speech service; the agent's name as wake word with CLI-shaped conversation; push-to-talk; spoken read-back; voice grammar per logbook; speaker verification so voice alone signs; audio retention |
| **E — Assurance** | Passkeys in the browser; personal-key signatures on records; stricter speaker-verification thresholds per logbook for voice confirmation; RFC 3161 timestamps on anchors (optional) |
| **F — Federation** | Cross-site verification via the federation directory; logbooks shared by a fleet with one accountable signer attesting across units |

## 10. Risks and open questions

- **Key custody out of process.** The signing operation (ADR-143) exists as
  `node_signer`: it fails closed and exposes only `sign`. The key still loads
  into the signing process. *Owner: identity/vault.*
- **Web re-authentication** (ADR-146) depends on the IdP honouring
  `max_age`/`prompt=login`. Confirm with UT's Entra configuration.
- **Wake-phrase reliability** in real rooms (radios, PA, conversation). It is
  measured per site before enabling. Push-to-talk is always available.
- **Migration of existing human acts.** Historical `decided_by` values cannot
  be proven human. They are imported as `import` records marked unverified,
  never as attestations.
- **Calibration of people.** Human-observed outcomes calibrate *models and
  rules*. Whether and how decisions by people are ever scored is a governance
  question for the site, not a platform default.
- **Regulatory mapping.** Map the record against electronic-signature and QA
  records expectations (signature manifestation: name, time, meaning;
  record-signature linking). *Owner: first regulated tenant.*

## 11. Acceptance and rollout

- Phase B is accepted when the first tenant logbook's pilot runs parallel logging
  on the site node with evidence packages verified by an independent script.
- Phase C is accepted when an approval from Steer produces an attestation
  linked from the executed action's receipt.
- Rollout starts with canary nodes; each phase ships with its spec section
  current.
