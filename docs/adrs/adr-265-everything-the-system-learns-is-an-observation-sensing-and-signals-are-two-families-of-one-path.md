# ADR-265: Everything the system learns is an observation; sensing and signals are two families of one path

**Status:** Accepted (2026-11-18). The owner endorsed the direction ("DRY out our system", covering every surface and agent) and decided the four open questions (D9 to D11, and the order below).
**Related:** [ADR-052](adr-052-database-tenancy-schema-per-extension.md) (schemas), [ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md) (logbooks), [ADR-163](adr-163-steer-and-log-are-two-projections-of-one-journal.md) (one journal), [ADR-182](adr-182-axiom-never-goes-down-from-its-own-causes.md) (expand-only migrations), [ADR-206](adr-206-silver-identity-is-the-reading-and-a-rebuild-replaces-in-place.md) (silver identity), [ADR-213](adr-213-upload-is-a-composable-extraction-pipeline.md) (extractor contract), [ADR-240](adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md), [ADR-252](adr-252-one-notification-path-a-cause-makes-an-incident.md) (incidents), [ADR-253](adr-253-an-ask-is-what-needs-a-person-needs-you-is-its-queue.md) (asks), [ADR-255](adr-255-a-node-is-local-site-or-edge-and-its-links-say-the-rest.md), [ADR-256](adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md), [ADR-263](adr-263-documents-and-agents-are-shared-by-drive-roles-an-agent-adds-invoker-and-a-run-acts-with-the-lesser-authority.md) (access labels), [ADR-264](adr-264-a-video-trigger-can-take-the-events-of-the-triggers-before-it-on-its-source.md) (chained triggers); issues #1797, #869

## Context

The platform learns things about the world along two trees that grew apart.

**Sensing.** An instrument's sensor yields a value; an edge node captures it
(DAQ `SignalEnvelope`, hash-chained), bronze holds the deposit, a normalizer
keyed by `schema_ref` conforms it into `silver.signals` (site, feed, channel,
ts, value, unit, quality, `source_class`, `model_ref`, `uncertainty`,
derivation, `row_hash`), gold serves it. Monitors, sentinels and video
triggers (`TriggerEvent`, ADR-264) derive events from it; an event that names
a cause becomes an incident (ADR-252).

**Signals.** The `signals` extension (agent SCAN) takes voice memos, meeting
transcripts, git diffs, calendars and notes from a JSON inbox, extracts
`Signal`s (source, timestamp, text, people, initiatives, type, a 0-1
confidence), correlates names to people and initiatives, and synthesizes
briefs and changelogs.

The owner's observation is that these are the same thing. Stage by stage they
are:

| Stage | Sensing | Signals |
|---|---|---|
| Source | instrument channel, declared per site | memo, transcript, diff, calendar, note |
| Capture | edge collector → bronze | inbox (not bronze) |
| Conform | normalizer registry → silver | `BaseExtractor` per file type |
| How sure | `uncertainty` in the value's unit, closed `quality` vocabulary | an extractor's self-reported confidence |
| Provenance | hash chain, `schema_ref`, `source_class`, `model_ref` | `originator`, free metadata |
| Correlate | none in silver; `incident_key` in video; HERALD fingerprints | name matching to people and initiatives |
| Derive | trigger events, monitor checks, incidents | blocker, decision, action item |
| Surface | charts, monitors, incidents | briefs, changelogs |

Because there is no shared contract, each new need has minted its own record.
An inventory of `main` in Axiom and its first domain product (2026-11-18) found:

| Record | Where | Overlap |
|---|---|---|
| `silver.signals` / `gold.signals` | Axiom data platform | the reading; name collides with the extension |
| DAQ `SignalEnvelope` | `data_platform/daq/envelope.py` | capture envelope for readings |
| `TriggerEvent` | `video/triggers.py` | a derived event with chained stages |
| `Incident` + `Cause` | `notifications/incidents/` | correlation by cause |
| `JournalEvent` | `journal/` (ADR-163) | the one index of what happened (refs, lanes, classification) |
| `Signal` (JSON) and `signals`/`media`/`participants`/`people` tables | `signals/` | statements from people and documents; its own people table; tables in `public` (#869) |
| `BaseExtractor` | `signals/extractors/` | duplicates the library's versioned extractor contract (ADR-213) |
| `Followup`/`FollowupEvent` | `program/` | action items with evidence |
| Memory `Fragment` + `Provenance` | `memory/` | bitemporal statements with rich provenance |
| a domain product's telemetry rows and timeseries table | product `telemetry/` | a second copy of the reading (removal already planned) |
| a domain product's twin frame | product `dtwin/` | a third reading shape |
| a domain product's sample events and custody | product `expman/` | a private event log (actor, time, payload) |
| a domain product's planned operations events and discrepancies | product ops-log spec | derived events (reading disagrees, operation unlogged), planned privately |
| a domain product's readiness findings and profile gaps | product `daq/` | derived events beside Axiom's gaps and expectations |
| logging `SignalSink`, CI-health `Signal` | `infra/axiom_logging.py`, `release/ci_health/` | name only |

The `signals` extension is the only place that turns unstructured human
input into structured claims and briefs. It is also the least maintained of
these trees: its Postgres tables are schema-only, its live pipeline is JSON
files, SCAN's persona promises correlation no code implements, and it holds
Postgres setup code that other extensions depend on. Retiring it would lose
the one capability nobody else has. Building on it as it stands would build
on its weakest parts. The abstraction worth building around is the one it
shares with sensing.

## Decision

**Everything the system learns about the world is an observation. An
observation is a reading, a statement or an event. All three travel one path
(capture, conform, correlate, derive, surface), each stage has one owner, and
no extension or product defines its own envelope.**

### D1: The observation contract

A contract (a typed shape and its validation, `axiom.observation`), not a
table. Every observation carries:

| Field | Meaning |
|---|---|
| `subjects` | typed references to what it is about: `channel:…`, `run:…`, `sample:…`, `person:…`, `initiative:…`, `document:…`, `host:…` |
| `observed_at`, `recorded_at` | when it was true and when the system learned it (bitemporal, as memory and the journal already are) |
| `source` | the producer (instrument, connector, extractor, trigger, agent, person) and the authenticated principal behind it |
| `source_class` | `measured`, `derived`, `predicted`, `simulated`, `stated` (a person said or signed it), `interpreted` (a model read it from text) |
| content | one family's payload (D2) |
| `uncertainty` | for a reading, in the value's unit with a `quality`; for an interpreted statement, a confidence calibrated per extractor version, and "not yet known" until a calibration exists (D10; a raw model self-score is never shown as one); for a stated one, none: it is attributed instead |
| `derivation` | the observations it came from, the method and its version (`schema_ref`, extractor manifest, trigger, `model_ref`), and a content hash |
| `labels` | access labels (ADR-263) |

### D2: Three families

- **Reading**: a quantity with a unit. Produced by instruments and models;
  high rate; identity is the reading (ADR-206).
- **Statement**: a claim from a person or a document: a decision, a blocker,
  an action item, a note, a logbook observation. `stated` or `interpreted`.
- **Event**: something that started, ended or occurred, usually derived from
  other observations: a trigger firing, a threshold crossed, a reading that
  disagrees with a signed log, a channel gone stale, a meeting decision.

A signed attest record is not an observation. It is a commitment and stays
the authoritative record (ADR-150). Which records emit an observation is
decided in D11.

### D3: One path, one owner per stage

| Stage | Owner | Readings | Statements and events |
|---|---|---|---|
| Capture | connectors, edge collector → bronze | DAQ envelope | the signals inbox becomes connector deposits in bronze |
| Conform | one registry of versioned conformers | normalizers (`schema_ref`) | extractors on the ADR-213 contract; `signals`' `BaseExtractor` retires |
| Correlate | a subject-resolver registry | channels, feeds | people through actors/directory (the `people` table retires), initiatives through `program`; a product registers its own subject kinds (for a facility: run, sample, configuration) |
| Derive | rules, triggers and agents that emit new events with `derivation` | sentinels, monitors, video triggers | SCAN's classification, a product's discrepancy rules |
| Surface | existing surfaces read observations | charts, monitors | the journal indexes every statement and event (ADR-163, refs not copies); an event with a cause is an incident (ADR-252); one that needs a person is an ask (ADR-253); a **brief** is a synthesis over a window and a set of subjects, across all three families |

### D4: Storage follows volume, not family

There is no single table. Readings stay in silver and gold. Statements and
events live in one store with its own schema (ADR-052), owned by the
extension that owns correlation and briefs. The journal indexes them and
stores nothing of its own.

### D5: The signals extension becomes `observations`, the owner of statements, events, correlation and briefs

The extension (renamed under D9) and SCAN keep their role and widen it: SCAN reads every family, so a
brief can say "the pool temperature crossed its limit during run 412, two
hours after the meeting that decided to raise power" from one store and one
resolver. What it sheds goes to its owner: capture to connectors, extraction
to the library contract, people to actors, its Postgres setup code to `db`.

### D6: Platform and product

Axiom owns the contract, the families, the path, the conformer and resolver
registries, and the store. A product declares subject kinds, statement and
event kinds, rules, and brief templates. A shift-handoff summary and a periodic report are briefs. Sentinels,
feed-versus-logbook discrepancies and readiness findings are event rules.
A product's telemetry and twin frames are readings. It defines no envelope of its own.

### D7: Every surface renders an observation one way

A surface is anything a person or a model reads observations through: the web
app, the CLI, MCP tools, chat, notifications (email, SMS, Slack, digest) and the
signing card. The rule is DRY at the boundary: **one envelope per family, one
renderer per family and form, shared by every surface.** `receipts` already
works this way: one deterministic brief composer feeds the CLI, MCP and the
web. That becomes the pattern for every family.

An inventory of main (2026-11-18) shows the cost of not doing this:

| Duplication | Today | Becomes |
|---|---|---|
| Reading envelope | `gold_query.envelope()` carries uncertainty; `medallion.envelope()`, `data latest` and a product's telemetry tools do not | one reading envelope (value, unit, `quality`, `uncertainty`, `claimable`, provenance, `chain`) from every reading tool, verb and endpoint |
| Reading tools | `axiom_data__series`, a product's `telemetry_series`, `axiom_daq__values`/`__view`: three shapes for the same readings | the `data` tools; a product registers channels, not tools |
| Briefs | `receipts/brief.py`, `signals/briefing.py`, `notifications.digest`, `program.digest` | one brief (D8); the four become templates (oversight, meetings, incidents, program) over one composer |
| Attention queues | landing attention chip, Needs you, Control's client-side needs-you facet | Needs you (ADR-253) is the one queue; the others are views of it |
| Timelines | journal Timeline and tail; video events in the DAQ journal; incidents and signals in their own tables | the journal indexes every statement and event; one timeline component |
| Derivation | two renderers (case derivation, Control's derivation panel) | one derivation component that shows inputs, method and version, and uncertainty |
| Quality words | defined three times with different words for a gap | one vocabulary, from the contract, shared by every surface |
| Series stores | a product's bucketed `/series` API and its telemetry gold, beside Axiom gold | Axiom gold through `data series` |

**What every surface must show, at its level of detail.** Progressive
disclosure: a quiet first look with detail one step away, never more fields
on the front.

| Field | First look | One step in |
|---|---|---|
| value or claim | always | – |
| uncertainty | `±` in the value's unit, or "not reported" (never blank, never zero); an interpreted claim's confidence, or "confidence not yet known" until calibrated (D10) | the coverage and how it was combined; for an interpreted claim, what the calibration was measured against |
| time | observed time, in site time | recorded time, when it differs |
| source | the source in a few words, with measured / modelled / stated / interpreted visible (a model's value is drawn dashed, as charts already do) | the full derivation: inputs, method and version |
| subjects | the run, channel, person or document it is about | links to each |
| access | nothing, unless held or restricted | why, and how to ask |

Today only Control and tables write `±` or "not reported". The chat value
card, chart readouts, stat tiles, brief rows, product series and every human
CLI output drop uncertainty, and series points lose it in the browser. Each of
these is a defect under this decision.

**CLI.** Each family has one human renderer shared by every verb that shows
it (`data latest`/`series`, `signal brief`, logbook verbs, `video events`,
`log`), and `--json` emits the envelope unchanged. A verb never invents its own
columns for time, source or uncertainty.

**MCP.** Tools are what a model reads, so:
- Every observation tool returns the family envelope. A model is told to
  report uncertainty with a value and never combine it itself, which only holds
  if every reading tool carries it.
- Tools are by family and verb (read observations, series, brief), not per
  tree. Fewer tools fit the tool budget and remove the `signals`, `data`,
  `telemetry` and `daq` collisions.
- Access labels filter results before a model sees them. A tool result lands
  in a transcript and in memory, so filtering afterwards is too late.
- A refusal or an empty window is structured (`claimable: false`, `chain`
  with the fix), never an empty list a model could read as zero.
- Advertised arguments are honoured: `axiom_signals__brief` advertises `kinds`
  and ignores it today.

**Entry points.** Where a person records an observation (the logbook
composer and signing card, `note`, voice memos, chat dictation, library
uploads, corrections, and the kiosk and voice console checks still being
designed), it is recorded as a `stated` observation in the shared store, with
the access a person chose. A reading entered by hand carries its resolution or
uncertainty when the entry kind declares one. A private note stays private by
its labels, not by living in a different store.

### D8: Agents produce attributable observations and share one noticing path

Our agents are both readers and producers of observations.

- **Produced, always attributed.** An agent's finding, diagnosis or
  classification is a `derived` or `interpreted` observation, attributed to the
  agent and its accountable person, with `derivation` naming what it read.
  Today SCAN's signals and KEEL's drift findings are recorded nowhere
  attributable, and PLINTH's alerts go out only through its own HERALD helper.
- **A model's claim is never re-read as fact.** When an agent's interpreted
  statement comes back through a surface or a transcript, it stays
  `interpreted` with its author. That is the loop signals' echo suppression
  guards today, now enforced by `source_class`.
- **One noticing path.** Noticing, deciding who hears and briefing are three
  jobs, and today several agents do all three on their own. Under this
  decision:
  - each specialist notices in its own domain and emits events: PLINTH for
    freshness, KEEL for drift, TIDY for vitals, TRIAGE for diagnoses, RIVET for
    CI, PULSE for schedules;
  - HERALD alone decides who hears (an event with a cause becomes an incident,
    one that needs a person becomes an ask);
  - SCAN alone composes briefs across families. CLERK's digest, HERALD's
    daily digest and the oversight brief become SCAN brief templates that
    CLERK, HERALD and receipts own the content of.
  No agent keeps a private feed of what it noticed.
- **A product's agents** (none exist today) follow the same rules: they
  register event rules and brief templates and add no envelope of their own.

### D9: Names: "signal" is retired

The owner's rule: the most correct name, the one that confuses no one. "Signal"
names three unrelated things today, so it names none of them.

| Today | Becomes | Why |
|---|---|---|
| (no umbrella term) | **observation** | The standard term (W3C/OGC SOSA: an act whose result estimates a property of a feature of interest). Our `subjects` are its features of interest. |
| `silver.signals`, `gold.signals` | **`silver.readings`, `gold.readings`** | In instrumentation a signal is a sensor's raw output; what the tier holds is the quantified value an instrument produced (ADR-256's sensor/instrument line). |
| the `signals` extension, noun `signal`, `axiom_signals__*` | **`observations`**, noun **`observation`**, `axiom_observations__*` | It owns statements, events, correlation and briefs (D5), not signals. |
| logging `SignalSink`, CI-health `Signal` | named for what they are (a log-to-bus forwarder; a CI failure pattern) | Neither is an observation. |
| SCAN | SCAN | An agent's name is a persona, not a term. |

Renames follow expand-only migration (ADR-182): the new tables and names come
first with the old ones as views or aliases for one release, every reader moves,
then the old names go. CLI verbs and MCP tools keep a deprecated alias for one
release that says the new name. The terminology ledger records each rename.

### D10: Interpreted confidence is "not yet known" until calibrated

No surface waits for calibration and none hides the field. The envelope
carries `confidence` and `calibrated` from the start. Until an extractor version
has a calibration (measured against corrections and confirmed outcomes), every
surface says **"confidence not yet known"** in the place the number will go, so
people learn where to look and what is missing. When a calibration exists the
same place shows it, with what it was measured against one step in. A raw model
self-score is never shown. This is the same rule as a reading's "not reported"
uncertainty.

### D11: Only formally signed logbooks emit observations

A logbook declares `emits_observations`. Formal logbooks whose records are
signed for the site (an operations log, a maintenance log and their kind) set
it; a personal logbook does not. An observation is emitted when a record is
**signed**, never from a draft. A typed value field emits a reading, and the
record emits a `stated` statement, both pointing at the record, attributed to
the signer, with the record's access labels. A co-signature or a supplement
emits a new observation that derives from the first; nothing is rewritten.

## Options considered

- **One table for everything.** Readings arrive at instrument rates and have
  their own identity (ADR-206). One table would make every statement query pay
  for them. Rejected.
- **Leave the trees separate; only fix the names.** Duplication keeps growing:
  the first domain product already has a second and third reading shape and
  two private event logs. Rejected.
- **The journal is the store.** ADR-163 makes the journal an index of refs
  with lanes, not a store of content. Rejected.
- **Retire `signals`.** Loses the only path from unstructured human input to
  structured claims and briefs. Rejected.
- **One contract, three families, storage by volume, one path (chosen).**

## Consequences and order

Each step is its own change, test-first, with expand-only migrations
(ADR-182).

1. **Name it (D9).** Add "observation", "reading", "statement" and "event" to
   the terminology ledger with every rename in D9. Park the schema move of the
   current `signals` tables (#869): those tables are replaced, not moved. Then
   the renames, each expand-first: `silver/gold.readings` beside
   `silver/gold.signals`, the `observations` extension and noun beside
   `signals`, and deprecated aliases for one release.
2. **The contract.** Add `axiom.observation`: types, validation and
   adapters that present a silver row as a reading, and a `TriggerEvent` or an
   incident's cause as an event. Add a journal lane for observations.
3. **Rebuild `signals` on it.** Add the statement and event store in its own
   schema; inbox to bronze; extractors onto ADR-213; people to actors; correlator
   to the resolver registry; brief across families. Move the Postgres setup code
   to `db`. Archive the legacy tables, then drop them one release later.
4. **Domain products.** Move telemetry into silver (the existing plan). Twin frames
   become readings. Sample events and the planned `ops.*` events become
   statements and events. Sentinels and readiness findings become event rules,
   and the handoff summary becomes a brief. Remove the demo's import of
   `signals` internals. Correct the glossary and spec-executive.
5. **Surfaces (D7), in parallel with step 2.** Make one reading envelope
   (`medallion.envelope` and `data latest` gain uncertainty), add one quality
   vocabulary and one uncertainty renderer to the app shell, and keep `±`
   through the browser into the value card, chart readout and stat tile.
   Add one CLI renderer per family. Retire the duplicate reading tools behind
   `data`. Add a test per surface: a value with an uncertainty shows it, and one
   without says "not reported".
6. **Briefs and queues (D7, D8).** One brief composer with templates; the
   landing attention chip and Control's facet read Needs you; one timeline and
   one derivation component.
7. **Agents (D8).** SCAN and KEEL findings become attributed events in the
   journal; PLINTH's alerts go through HERALD's path; digests become SCAN
   templates.
8. **Logbooks (D11).** `emits_observations` on the logbook declaration;
   signing a record in a formal logbook emits its reading and statement.

**Schedule.** Steps 1, 2 and 5 start now, ahead of R6, so the first domain
product builds its operations-log events on the contract rather than
privately. The rest follows in R6 (data platform and RAG, #1797).

The unbuilt surface specs (Needs you, Log and Control, embodied surfaces,
video events, notifications, the chart surface, and a product's ops-log and
console-check screens) adopt D7 before they are built. None of them mentions
uncertainty today, and a console-check mock-up shows a bare "confidence"
score.

## Decided by the owner (2026-11-18)

1. **Names:** the most correct name, the one that confuses no one (D9).
2. **Calibration:** not a gate; pave the way so confidence appears when it is
   available, and say "not yet known" until then (D10).
3. **Logbooks:** only formally signed logbooks, such as an operations log or a
   maintenance log (D11).
4. **Schedule:** start now (see the schedule under Consequences).
