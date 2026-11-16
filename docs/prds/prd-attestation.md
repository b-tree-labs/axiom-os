# PRD: Attestation — the record of accountable human acts

**Owner:** Axiom platform • **Status:** Draft • **Last updated:** 2026-09-29
**Spec:** [spec-attestation.md](../specs/spec-attestation.md)
**ADRs:** [142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md) (the primitive),
[143](../adrs/adr-143-attestations-are-signed-into-a-verifiable-chain.md) (signed chain),
[144](../adrs/adr-144-a-person-confirms-exactly-what-is-recorded.md) (confirmation),
[145](../adrs/adr-145-voice-stays-on-site-and-a-spoken-answer-signs.md) (voice),
[146](../adrs/adr-146-signing-from-the-browser.md) (browser signing),
[147](../adrs/adr-147-surfaces-receive-live-updates-over-sse.md) (live updates), [150](../adrs/adr-150-an-attestation-record-is-kept-in-a-logbook.md) (logbook, not book), [243](../adrs/adr-243-people-see-a-log-code-keeps-logbook-and-log-is-the-cli-noun.md) (people see "log"; `log` is the command noun; proposed)
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

## 6b. The command line: `log`

*Accepted with [ADR-243](../adrs/adr-243-people-see-a-log-code-keeps-logbook-and-log-is-the-cli-noun.md).
The ADR holds the decisions and how the noun is built; this section is what a person and an
agent see. **Built (unit F5)** for the rows marked F5, including the verbs units L1 to L5, W3
and X2 had added under `axi attest`; as built in spec-attestation "As built: the `log` noun".
Each later unit adds its verb (table A).*

*As built, beyond the tables below: `log due --tick` (`attest.obligations_tick`), `log access`
and `log <log> access` (`attest.access`, unit X2), `log verify-all` (`attest.verify_all`),
`log <log> unacknowledged` and `log <log> cosign-pending` (units L3 and L4, until `log waiting`
gathers them), and `log waiting --log LOG` (what `attest drafts --logbook` filtered).
`log platform trace` is reserved and not built: the system log had no `trace` verb.*

People say "log", so the command says `log`: the same word as the Log page. Code keeps
`logbook` (manifest, routes, skills, MCP tool names); no help line, prompt or message says
"logbook" or "attestation" unless it names code.

### Grammar

```
<cli> log <verb>                    things about all logs on this node
<cli> log <log> <verb> [args]       one log, named by its handle
<cli> log platform <verb>           Platform events (the machine's own log)
```

`<cli>` is the product's brand (`axi`, or a consumer's). **A log is named by position, not by a
flag:** `axi log demo show`, not `axi log show --log demo`. Reasons: it reads the way people
say it ("log, demo, show"), which is also the voice grammar (§6 item 8: the agent asks for each
next level); Tab completes the handle before offering the verbs; and every verb of a log needs
one, so a required flag would only be noise. The handle is declared in the log's file
(`handle = "demo"`, default its id). Words used by noun-level verbs and `platform` are reserved,
so a handle never shadows a verb.

**Flags every verb takes**

| Flag | Meaning |
|---|---|
| `--json` | Machine output: the skill's result as JSON, nothing else on stdout |
| `--site SITE` | Which site's chain; default the bound site |
| `--since T`, `--until T` | Time range: ISO date or time, or relative (`2h`, `3d`) |
| `--limit N` | At most N rows (default 20) |
| `--type`, `--signer`, `--interval N`, `--meaning`, `--text` | Filters, on the read verbs that list records |

**Exit codes:** `0` done; `1` refused, or a check failed (a broken chain, a refused signature),
with the reason on stderr; `2` usage (unknown log, verb or flag), with the nearest match;
`3` nothing was signed because the person held, asked or did not answer.

**Help text style:** one line per verb, sentence case, saying what happens in the person's
words ("Sign a draft that was proposed for you"); a write verb says whether it signs; every
`--help` and every result ends with a `next:` block of two read commands (spec-axi-cli
"Examples and `next:` lines"). A refusal names who may do it instead.

### Verbs, in one table

Each verb calls one skill (ADR-056). **Person** means only a person at the prompt can do it;
**propose** means software may only make a draft for a person; **read** means anyone allowed to
read the log, on every surface.

| Verb | What it does | Who | Unit |
|---|---|---|---|
| `log list` | The logs here: who writes, who signs, what each holds | read | F5 |
| `log waiting` | What waits for me: drafts proposed for me, co-signatures, acknowledgements | read | F5, L1, L3, L4 |
| `log timeline` | Everything that happened, across logs and Platform events, filtered by role | read | J1, J2 |
| `log <log> about` | The log's declaration: entry types, roles, rules, retention | read | F5 |
| `log <log> show [ID]` | Latest entries, or one entry with its thread and proof | read | F5, L2 |
| `log <log> search` | Find entries by time, type, signer, interval, text | read | W3 |
| `log <log> new TYPE` | Write an entry and sign it at the prompt | person | F5 |
| `log <log> propose TYPE` | Make a draft for a named person; never signs, never fills observed fields | propose | L1 |
| `log <log> sign DRAFT` | Read a draft, complete its observed fields, answer sign, hold or ask | person | F5 |
| `log <log> supplement ID` | Correct, clarify or add to an entry | person (agents propose) | L2 |
| `log <log> acknowledge ID` | Say I have read an entry | person | L3 |
| `log <log> cosign ID` | Co-sign an entry | person | L4 |
| `log <log> seal` | Close a period with a summary and the chain head | person (agents propose) | L5 |
| `log <log> export` | Write the log as JSON Lines, text, CSV or a declared form | person | F5, L6 |
| `log <log> import --from SOURCE PATH` | Bring in old records, marked "imported, not signed" | person with import rights | O7's Axiom half |
| `log <log> verify` | Check the chain, end to end or from a sequence | read | F5, X2 |
| `log <log> intervals [N]` | Runs, shifts or work orders (the log's own word) | read | W3 |
| `log <log> due` | What is due and what was missed | read | F5 |

**Present, hold and ask are not verbs.** `new` and `sign` present the exact entry, and the person
answers **sign**, **hold** (keep it as a draft) or **ask** (send it back with a question) at the
prompt: one decision, one place (§6 item 7). Over HTTP the same steps are the present and respond
routes. Administration (`log role`, `log device`, `log location`, `log anchor`, `log validate`)
is listed in table A.

**What software may never do** (ADR-142, ADR-144): no `sign`, `cosign`, `acknowledge`, `seal` or
`supplement` skill is on `mcp` or `agent_tool`; agents use `propose` (and the propose forms of
supplement and seal), and a proposal never carries a value for an observed field. A person
signing at the CLI is the person who typed: the library requires `entered_by` for a human-origin
draft and refuses observed fields without it (F1 note Q9), records `source = "library"` for
library signatures (Q4), and refuses reads and writes for a site the node does not serve (Q10).

### A consumer's own verbs

A consumer layer adds **no verbs under `log`**. What its log needs from a generic verb, it
declares in the log's file: export forms (`export --form NAME`), import sources
(`import --from NAME`), the word for an interval ("runs"), entry types and their fields. Views of
the consumer's domain that read the log among other sources (current state, plans, equipment in
use) belong to the consumer's own noun, which never means the log. This keeps one grammar for
people, agents, completion and speech.

### Platform events

The machine's own log keeps its verbs under `log platform`: `tail [--follow]`, `verify`,
`stats`, `export`, `trace`, `backend`, `sinks` ([prd-logging.md](prd-logging.md)). It is not
signed and has no write verb. Its rows also appear in `log timeline` as the `platform` lane.

### Moving from today's commands

Old forms keep working for two minor releases after `log` ships, print one stderr line naming
the new form, and are hidden from help (`AXI_NO_DEPRECATION_WARNING=1` silences the line).

| Today | From F5 |
|---|---|
| `axi attest logbook list` | `axi log list` |
| `axi attest logbook validate FILE` | `axi log validate FILE` |
| `axi attest new --logbook L --type T ...` | `axi log L new T ...` |
| `axi attest sign DRAFT` | `axi log L sign DRAFT` (L read from the draft) |
| `axi attest show [ID] --logbook L` | `axi log L show [ID]` |
| `axi attest verify --logbook L` | `axi log L verify` |
| `axi attest obligations --logbook L` | `axi log L due` |
| `axi attest export --logbook L --out DIR` | `axi log L export --out DIR` |
| `axi attest anchor`, `device ...`, `role ...`, `location ...` | `axi log anchor`, `log device ...`, `log role ...`, `log location ...` |
| `axi log tail\|verify\|stats\|backend\|sinks\|export\|trace` | `axi log platform <same verb>` |

MCP tool names (`axiom_attest__*`) and `/api/v1/attest/...` routes do not change.

### Example sessions (fictional names)

An operator, Dana Reyes, at the start of a shift:

```
$ axi log waiting
  demo  draft 7f3c  ROUND_CHECK proposed by @rounds-bot for you, 06:58
$ axi log demo sign 7f3c
  ROUND_CHECK, Round check. Pressure (observed): _
  Pressure, bar: 4.2
  Sign, hold or ask? sign
  Signed #412 as performed by @dana.reyes.
  next: axi log demo show 412 · axi log demo due
```

An agent, `@rounds-bot`, through MCP: it calls `axiom_attest__due` for the log `demo`, sees a
round check due at 07:00, and calls the propose tool for Dana with the title only. Pressure is
observed, so the proposal leaves it empty; Dana's tray shows it.

A reviewer, Lee Okafor, at the end of the week:

```
$ axi log demo search --type ROUND_CHECK --since 7d --signer @dana.reyes
$ axi log demo verify
  Chain intact: 1-430, head 9e1d...
$ axi log demo export --since 7d --format jsonl --out ./week-46
```

### Appendix A: every verb, its skill and its route

| CLI | Skill | Surfaces | HTTP | Unit |
|---|---|---|---|---|
| `log list` | `attest.logbook_list` | cli, mcp, agent_tool | `GET /api/v1/attest/logbooks` | F5 |
| `log waiting` | `attest.drafts`, `attest.cosign_pending`, `attest.unacknowledged` | cli, mcp, agent_tool | `GET /api/v1/attest/drafts` and the L3/L4 routes | F5 (drafts), L1, L3, L4 |
| `log timeline` | `journal.query` | cli, mcp, agent_tool | J1's journal route | J1, J2 |
| `log timeline lanes` | `journal.lanes` | cli, mcp, agent_tool | J1 | J1 |
| `log validate FILE` | `attest.logbook_validate` | cli | none | F5 |
| `log anchor` | `attest.anchor` | cli | none | F5 |
| `log role grant\|revoke\|list` | `attest.role_*` | cli | none | F5 |
| `log device enroll\|reclaim\|retire\|list` | `attest.device_*` | cli | none | F5 |
| `log location init\|code` | `attest.location_*` | cli | none | F5 |
| `log due --tick` | `attest.obligations_tick` | cli | none | F5 |
| `log <log> about` | `attest.logbook_list` (one log) | cli, mcp, agent_tool | `GET /api/v1/attest/logbooks/{logbook}` | F5 |
| `log <log> show [ID]` | `attest.show` | cli, mcp, agent_tool | `GET .../{logbook}/records`, `.../records/{id}` | F5 |
| `log <log> search` | `attest.search` | cli, mcp, agent_tool | `GET .../{logbook}/records?...` | W3 |
| `log <log> intervals [N]` | `attest.intervals` | cli, mcp, agent_tool | `GET .../{logbook}/intervals` | W3 |
| `log <log> due` | `attest.obligations` | cli, mcp, agent_tool | `GET .../{logbook}/obligations` | F5 |
| `log <log> verify` | `attest.verify` | cli, mcp, agent_tool | `POST .../{logbook}/verify` | F5 |
| `log <log> new TYPE` | `attest.new` | cli | `POST /drafts`, `/present`, `/respond`, `/sign` | F5 |
| `log <log> sign DRAFT` | `attest.sign` | cli | `/drafts/{id}/present`, `/respond`, `/sign` | F5 |
| `log <log> propose TYPE` | `attest.propose` | cli, mcp, agent_tool | `POST /api/v1/attest/proposals` | L1 |
| `log <log> supplement ID` | `attest.supplement` (person), `attest.supplement_propose` (software) | cli; propose on mcp, agent_tool | `POST .../records/{id}/supplement` | L2 |
| `log <log> acknowledge ID` | `attest.ack` | cli | `POST .../records/{id}/ack` | L3 |
| `log <log> cosign ID` | `attest.cosign` | cli | `POST .../records/{id}/cosign` | L4 |
| `log <log> seal` | `attest.seal` (person), `attest.seal_propose`, `attest.seal_preview` | cli; propose and preview on mcp, agent_tool | `POST .../{logbook}/seal`, `GET .../seal/preview` | L5 |
| `log <log> export` | `attest.export`, `attest.export_preview` | cli (preview also mcp, agent_tool) | `POST/GET .../exports` | F5, L6 |
| `log <log> blank-form FORM` | `attest.export_blank` | cli, mcp, agent_tool | `GET .../{logbook}/forms/{form}/blank` | #1805 |
| `log <log> import --from SRC PATH` | `attest.import` | cli | O7's route | O7 (Axiom half) |
| `log platform tail\|verify\|stats\|export\|trace\|backend\|sinks` | `log.tail`, `log.verify`, `log.stats`, `log.export`, ... | cli | none | F5 |

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
