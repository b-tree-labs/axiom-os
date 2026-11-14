# Product Requirements: The Site View

**Product / Feature:** The Site view: a site as a system of systems, with its loops

**Owner:** Axiom platform   •   **Status:** Draft for review   •   **Last updated:** 2026-10-30

**Design:** [`spec-site-view.md`](../specs/spec-site-view.md) (how it is built)   •   **Decisions:** ADR-209 (every installation is a site, proposed), ADR-050, ADR-211 (Control), ADR-214 (one chart spec)   •   **Mockups:** the Site section of the master design, https://claude.ai/artifact/4Wtqj1voZMCdw56C24HHLv (private; the owner shares it; a convenience view that may lag, not the source), light and dark; the generic design entry and the icon set are in appkit, [`docs/design/site-view/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/site-view)

> **Decisions since this draft (2026-11-10).** Where this document disagrees with
> these, these win: the rail is Chat · Site · [Model] · [Chart] · Log · Library · Team,
> with History at the bottom; Model and Chart are shown by user type; Team is universal,
> on every rail by default, and a person or site may switch it off; a product inserts its
> items after Site; a hidden item stays reachable through Search and ⌘K and through a
> More item at the bottom of the rail (shown only while something is hidden, each item
> with Pin to rail), and Settings is where the rail is changed; Search is a full-width row under the
> wordmark plus ⌘K, not a rail item (master design D1, D11; ADR-223 as amended);
> Control is off the rail: it is Site › Automate, keeping its route `#/control` and its
> ⌘K name, and Site's modes are Operate, System and Automate (D27); "Steer" stays
> retired; the accent is the one interactive colour and a brand's identity colour is
> never interactive or a status, and controls are rounded from appkit's tokens (ADR-226
> as amended); people see "log", code keeps `logbook`, and `log` is the command noun
> (ADR-243); a person's Book follows domain and roles and only grows (ADR-239); a record
> lives with its writer, and a page reads its own tables and reads gold only read-only
> with an "as of" (ADR-241). Earlier rounds below are kept as the record.

---

## 1) Elevator Pitch

A site is not its data flow. It is a **system of systems**, like a body with
circulatory, nervous and skeletal systems: the physical process it exists to
run, the instruments that watch it, the pipelines that carry what they see, the
models that predict it, the people on shift, the authorizations it runs under,
and the platform underneath. Each of these keeps itself working through
**loops**: control, maintenance, calibration, scheduled jobs, compliance, and
the improvement loop that makes predictions better.

The Site view puts all of that on one page, with its own nav rail home. It
answers *"is this site healthy, and if not, why?"* from one evaluation that
every surface reads, so that no two views ever disagree. For any value on the
screen, measured or predicted, it shows where the value came from. And it lets
a person see the site three ways: **Physical** (the plant, with data, water and
power flowing), **Systems** (health and loops) and **Data** (the raw values).

## 2) Problem / Opportunity

- **Data flow is one system, not the site.** Today the only site-level view is
  the data topology. It cannot say whether the instruments are calibrated,
  whether the model being served is valid for the plant's configuration, or
  whether a maintenance window explains a gap.
- **Freshness is not health.** The first real capture (2026-10-30, 00:06 UTC)
  makes the point. Every measured channel of the first site had held one value
  for 3.4 days while rows kept arriving every second. The model's predictions
  held from the same moment, and they were still being published. A
  freshness-only check would have called both pipelines healthy for three days.
  The model's second output had been silent for two weeks, unnoticed.
- **Views disagree.** A footer, a page and an attention queue that each
  evaluate health separately will eventually contradict one another.
- **Predictions are not attributable on screen.** A display that shows a
  prediction without saying which model, for which configuration, under which
  decision, and how fresh, invites *mode confusion*: the classic control-system
  hazard where a person believes the automation is doing one thing while it
  does another.
- **Opportunity.** The platform already has most of the pieces: site records
  and visibility (ADR-209), heartbeats and topology, the medallion with
  provenance, a scheduler with a fire log, attestations, the ModSim engine, a
  data table and a chart spec with 3D. The Site view composes them into one
  surface, contributed by extensions, so every product that builds on Axiom
  gets it and specializes it without forking.

## 3) Goals & Success Metrics

| Goal | Measure |
|---|---|
| A person can tell in seconds whether a site is healthy and why not | root causes listed first; one failure reads as one item, with its consequences folded under it |
| No two surfaces disagree | the chip, the drawer, the rail home, CLI, MCP and Control read one snapshot; a test asserts equality |
| Held, silent and late data are caught | the 2026-10-26 hold would have been raised within the declared max-hold (10 minutes), not missed for days |
| Any value is traceable | lineage resolves for every served value on the v1 pipelines, with unrecorded steps shown, never dropped |
| Products specialize without forking | each domain product expresses its Site entirely through contributions |
| Unknown is never green | every state that cannot be established renders `unknown` with a reason |

## 4) Key Users / Personas

| Persona | Their question | Where they look |
|---|---|---|
| **Operator or shift lead** | Is what I'm seeing true right now? Which model is this prediction from, and is it valid for the plant as it is? | Physical mode, the status chip, a value's lineage |
| **Model owner or modeller** | Are my model's inputs and outputs flowing? Is error going down across revisions? What should I try next? | the predicted pipeline, Model improvement, lineage |
| **Data steward (or the data agent)** | Which hop is broken, held or late, and since when? | the pipelines drill-in, Loops, Control |
| **Site manager** | What's overdue across everything: calibrations, surveillances, backups, renewals? | Loops, the roll-up |
| **Advisor or program lead with many sites** | Which of my sites needs attention? | the scope selector, the All sites home |
| **Engineer extending a product** | How do I add my domain's systems, loops, labels and drawings? | the spec's extension points |

## 5) Scope: Key Capabilities

### 5.1 A site is a set of systems, on a control-structure skeleton

Each system is contributed by an extension, the way the Library takes
previewers and extractors. Every system has the same base: identity, health
(`ok`, `degraded`, `broken`, `unknown`, always with reasons), an inventory of
members, relations to other systems, a summary card and a drill-in view.

The systems are arranged by the **STPA control structure** (Leveson, 2011):
controllers act downward through controls and actuators on a controlled
process, and learn about it upward through sensors, pipelines, estimators and
displays. That skeleton is domain-general (used for aircraft, factories and
hospitals). It places every system, and it gives health a natural axis:
**feedback** (can the people and automation in charge see the process truly?)
versus **control** (what acts on it, which the platform observes but never
provides).

| Position in the control structure | Systems that fill it |
|---|---|
| Constraints from above | licenses, permits and obligations; access and security |
| Human controller: procedures, models of the process, the automation and the context | people and shifts; documents and records; operations (schedule, campaigns, mode) |
| Controls and actuators | control and actuation, **observed only**: the platform issues no commands (a stated boundary) |
| Automated controller | control and actuation (observed); a model promoted into the control path by a recorded decision |
| Controlled process | assets and machines |
| Sensors | instruments and sensors |
| Real-time feedback channel | data pipelines, kind `stream` (into estimators and models) |
| Batch feedback channel | data pipelines, kind `batch` (the medallion, to displays and history) |
| Estimators | models and twins, with the operating configuration |
| A controller of the models | model improvement (§5.5) |
| Displays | the app itself; agents and AI that advise |
| Disturbances | environment |
| Supporting | maintenance; supply and inventory; compute and infrastructure |

**The initial catalog** (sixteen system types; the catalog is open):

| # | System | Covers |
|---|---|---|
| 1 | Data pipelines | every path a value takes, in kinds: stream, batch, predicted, and later residuals and training |
| 2 | Instruments and sensors | declared devices, placement, channels, calibration state |
| 3 | Assets and machines | the physical things the site runs, as a part-of tree |
| 4 | Operations | mode, state, runs, campaigns, schedules |
| 5 | Models and twins | predictive models, their revisions, inputs, readiness level |
| 6 | People and shifts | who is on, in which role, qualified for what |
| 7 | Access and security | who may enter or touch what |
| 8 | Licenses, permits and obligations | what the site may do, and what it owes |
| 9 | Documents and records | procedures, logbooks, reports (the Library) |
| 10 | Agents and AI | the agents acting at the site, and their liveness |
| 11 | Maintenance | work orders, inspections, calibration due |
| 12 | Environment | conditions around the site that bear on it |
| 13 | Supply and inventory | consumables and materials on hand |
| 14 | Compute and infrastructure | nodes, hosts, relays, storage |
| 15 | Control and actuation | controllers, controls, actuators, observed only |
| 16 | Model improvement | residuals, targeted losses, hypotheses, training, promotion |

Health rolls up through relations. A failure that causes others is listed once
as a **root cause**, with its consequences folded under it, and the site page
leads with *"Why is this site broken?"* A broken member raises one Control item;
consequences do not raise their own.

### 5.2 Loops are a first-class dimension

Systems say what a site is made of; loops say how it keeps itself working. A
loop runs in cycles of **trigger** (time, event or threshold), act, observe and
evaluate. It has an owner, a cadence or trigger, a cycle history, and health:
on time, due, overdue or missed; the last outcome; and whether the quantity it
exists to move is converging or drifting.

| Loop | Trigger, act, observe, evaluate |
|---|---|
| Control | measurement, control action, response, error (the STPA feedback path) |
| Improvement | residuals past a threshold, hypothesis and training, validation, the targeted loss |
| Maintenance | due date or condition, work, inspection, sign-off |
| Calibration | due or drift detected, calibrate, check against reference, accept |
| Scheduled | time, a platform job (freshness reports, connector refreshes, backups and their validation, node probes, nightly rebuilds), its receipt, the outcome |
| Operating cycle | a plan or a start, operate, record, hand over (shift handover; startup to shutdown; a season; a production run) |
| Compliance | a due date, perform or attest, record, verify (surveillance tests, renewals, attestations, expiring credentials) |
| Data quality | expectations, marks, rebuild, verify |
| Corrective action | an incident, triage, fix, verify the fix held |
| Replenishment | stock below a reorder point, order, receive, count |
| Release | a release, deploy blue/green, verify, keep or roll back |

Loops run **through** systems: each system's drill-in lists its loops. Loops
**couple**: maintenance takes an instrument offline, which marks its readings,
which the improvement loop then excludes. An overdue calibration invalidates
residuals computed since. Couplings feed the roll-up, so calibration drift is
not mistaken for model error. A **Loops** view answers *"what is overdue, across
everything?"*

### 5.3 Two measured channels, a predicted one, and reconciliation

An instrumented value usually travels two ways:

- **Real-time (stream):** sensor, acquisition, straight into a consumer's input
  (an estimator, a model, a controller). It is hyperlocal and low-latency, and
  it does not pass through the medallion. Its health is measured in seconds:
  latency, gaps and drops, staleness at the consumer's input, consumer
  liveness.
- **Batch (medallion):** sensor, acquisition, transport, ingest, bronze, silver,
  gold. It serves charts, history, training, validation and replay. Its health
  is measured in minutes to hours: hop freshness, row arrival, held values.
- **Predicted:** a model's output. A model is valid only for the configuration
  it was built for, so its health is a chain: observations, configuration
  estimate, selection (select, transition or refuse, with the reason), model
  revision, then delivery. **Held values are never published:** a model with no
  fresh, qualified configuration says so rather than repeating its last answer.

The two measured channels are shown **side by side**, since one can be healthy
while the other is not. The same reading should arrive on both and agree within
tolerance. **Reconciliation** of the two is its own health signal: a missing
twin means a channel drops, a disagreement means one alters. It is also how a
prediction made from real-time inputs is audited later against gold.

### 5.4 Lineage of any value

For any value on screen, the path back to its origin, with quality flags and
uncertainty where they are known (and `claimable: false` where they are not):

- **Measured:** the value names its channel. Batch: gold, silver, bronze,
  transport, collector, acquisition, instrument. Stream: consumer input, stream,
  acquisition, instrument. Each links to its twin on the other channel.
- **Predicted:** model revision, configuration key and the probability it was
  selected under; the operating configuration's variables; the observations
  behind them, each naming the channel that fed the model and linking to its
  batch twin; and the instruments.
- **Later, past the model revision:** training set, simulation runs, input deck,
  hypothesis, residual dataset. And **forward** from a hypothesis: what it
  changed, and whether the loss dropped.

A step that cannot be established is shown as "not recorded", never dropped.

### 5.5 The improvement loop

This loop is why a predictive model exists. Measured and predicted values are
joined by reading identity and configuration into **residual datasets**. Someone
names a **targeted loss** to reduce. A **hypothesis**, a signed, attributable
record, claims a change will reduce it and cites the residual evidence. The
change becomes a modified simulation input deck; simulation runs produce a new
training set; training produces a **candidate revision**. The candidate is
validated against the targeted loss and checked for regressions, and a person
promotes it up the readiness ladder (model, shadow, twin). New predictions make
new residuals, and the loop turns again.

The **Model improvement** system shows each targeted loss and its trend across
revisions (*is error going down?*), open hypotheses, training runs in flight,
candidates waiting, and regressions. It reuses the ModSim engine (run records,
verdicts, slots) and the model registry; it does not invent them.

### 5.6 Three view modes, with the selection kept

A toggle on the Site view switches modes. **The selection is kept across
modes**: click an instrument in Physical, switch to Data, and it is still
selected.

| Mode | For | Shows |
|---|---|---|
| **Physical** | seeing the plant | a mimic diagram: 3D geometry, a 2D schematic, or a map, with instruments pinned where they are and **animated flows** of data, water and power between them, including where unseen things come from and go. Muted base; colour only for abnormal state (ISA-101), so motion is not noise. A broken hop shows as a break; an unknown one is dashed and never animated. **Click** anything for its live value, a sparkline, health and lineage. Respects reduced-motion settings |
| **Systems** | health and loops | the card grid or the control-structure layout, the roll-up, the Loops view |
| **Data** | working with raw values | the platform data table: filters, compare, export, copy-as-code |

A site with no physical representations has no Physical mode.

### 5.7 Scope: one site or many

A person may see one site or many. A **scope selector** at the top of the Site
rail picks one, or "All sites" for browse pages. It ports the advisor scope
pattern proven in an earlier product, where an advisor who works with many clients
picks one at a time:

- search and groups;
- the person's own sites first;
- a health dot per site;
- counts by state on the "All sites" row.

The server decides which sites a person may see, from ADR-209's authorization
model. A site outside that set is indistinguishable from one that does not
exist. Drill-ins and lineage are always single-site.

### 5.8 The ambient entry

A **site status chip** sits in the rail foot of every page of the app. It
reads, for example, "site broken · 2". It opens a drawer with the roll-up for
this site, then every site in scope with a strip of system states. This keeps
the footer chip and drawer the owner chose earlier (option D), now covering all
systems.

### 5.9 Three kinds of site, side by side

The same model fits a process plant, a farm and a factory. A product for each
supplies its systems, loops,
labels and drawings as contributions (§5.10).

| | Process plant | Farm | Factory |
|---|---|---|---|
| **Site** | a plant and its experimental facilities | an operation: its fields and management zones | a plant and its lines |
| **Scope selector** | a program lead across several plants | an advisor across client operations (the original pattern) | a regional manager across plants |
| **Controlled process (assets)** | the process unit and its configuration; vessels, loops, experimental facilities | fields (versioned boundaries) and management zones; the crop on each field | lines, cells, machines |
| **Instruments** | detectors, temperature probes, position encoders, console capture | satellite imagery, gridded weather, equipment connectors (yield and as-applied), soil lab results; in-field probes are a later instrument class | PLC tags, vision stations, gauges |
| **Stream channel** | acquisition straight into the surrogate model's input | none today (every farm input is batch); a probe into irrigation logic would be one | PLC tags into the MPC controller |
| **Batch channel** | the medallion, for charts, history and training | the lakehouse: imagery, weather, connector syncs and lab uploads to bronze, silver, gold | the historian, for quality dashboards |
| **Models and twins** | reduced-order surrogate at the shadow rung, selected by the operating configuration | soil-property prediction, zone generation, prescriptions, ROI; conformal calibration with a calibration gate | throughput and defect models |
| **People** | licensed operators on shift | growers, advisors, labs | line leads, maintenance techs |
| **Obligations** | operating license, surveillance requirements, export-control holds | consent records (the data-sharing agreement for model training); applicator licenses and water rights where a region requires them | environmental permits, audits |
| **Loops** | startup and shutdown; surveillance; calibration; the model improvement loop | the crop cycle (planting, emergence, harvest); the sampling plan, lab order, results and retrain cycle; scheduled imagery and weather pulls; connector syncs; recalibration from harvest-versus-forecast and lab residuals | production runs; preventive maintenance; process-model recalibration from line residuals |
| **Physical mode** | the process unit in 3D; a loop schematic | a field map (GeoJSON) with zones, sample points and imagery layers | a P&ID |
| **Book (inside the Library)** | the product's book | the product's book | the Data Book |

### 5.10 Products specialize the Site without forking

A product adds system types, extends or relabels generic ones, contributes
loops, health rules and physical representations, and overrides vocabulary.
All of it is done through declared extension points, in the same way it already
relabels the Library's Data Book. A site repository then adds its own drawings,
anchors and thresholds. Nothing in Axiom names a domain. The extension points
are specified in the spec. Each product's specialization is described in its own
Site Operations PRD.

## 6) Non-Functional / Constraints

- **Domain-free platform.** Axiom names no domain and no customer. Products and
  site repositories supply all domain content.
- **Unknown is never green.** Absent stays absent: no zero for a missing value,
  no default uncertainty.
- **Read-only.** The Site view observes and diagnoses. It never commands
  equipment, and it has no write path to a site's configuration (that is
  ADR-209's).
- **One evaluation.** The evaluator runs where the data lives. A bound app
  forwards to the site's node.
- **Scope on the server.** An unauthorized site id returns 404, not 403.
- **Accessibility.** Light and dark modes. Colour is never the only signal.
  Animation respects reduced motion.
- **Export-control and security content** stays summary-level on any view; the
  consumer decides what a member may expose.

## 7) Timeline and Phases

**Version 1 answers one question.** *Are the measured pipelines (the real-time
stream into the surrogate and the batch medallion path) and the predicted
pipeline healthy right now, and for any value I am looking at, where did it
come from?*

| Version 1 builds | Detail |
|---|---|
| The base system and loop contracts, registry and conformance | so every later system or loop is a contribution, not a rewrite |
| The evaluator, the snapshot, and Control items for broken members | one evaluation, many readers |
| **Data pipelines**, fully evaluated | `measured_stream`, `measured_batch` and `predicted`. Freshness, row arrival, **held-value** and idle-versus-broken checks; reconciliation; the §5.3 chain for predicted |
| Stream telemetry, **stubbed honestly** | no stream health record is exposed today, so stream hops are `unknown`. Needed: the acquisition-to-model link publishes a small health beat (last consumed reading per channel, latency, gaps). Until then the stream is never drawn as healthy |
| Instruments, models, compute | inventory only, excluded from the roll-up, so the relations have endpoints |
| Lineage | measured back to the instrument; predicted back through revision, configuration and observations; unrecorded steps shown |
| Records the later phases need | reading identity, configuration key and model revision kept wherever a producer supplies them, so later phases join rather than rebuild |
| Scheduled loops of the pipelines system | from the scheduler's definitions and fire log, presented with the loop contract |
| Scope selector, rail home, pipelines drill-in, lineage drawer, chip and drawer | the surfaces in the gallery |
| View modes | Systems and Data; Physical as a 2D schematic of the v1 pipelines with animated, clickable flows. This is a modest SVG over data the evaluator already has |
| A prerequisite fix | a bound app forwards every per-site route to the site's node. Today its topology and health report its own (nearly empty) chain |

| Phase | Adds |
|---|---|
| **2** | Physical mode's 3D geometry and maps with instrument anchors; the **Loops** view across all systems; stream hops and reconciliation fully evaluated wherever the stream publishes its beat; configuration and selection hops wherever a consumer publishes them; health for instruments, models and infrastructure; health history; the All sites portfolio |
| **3** | maintenance, calibration, operating-cycle and compliance loops with their couplings; residual datasets; operations, people and shifts, licenses and obligations, with the `requires` relation (an operating mode that lacks a qualified shift or a license in force is a reason) |
| **4** | the **Model improvement** system and its pipelines (residuals, training, validation and promotion); hypotheses as signed records; lineage past the model revision and forward from a hypothesis; documents, agents, environment, supply |
| **5** | a guide to building a site system or loop; program-scoped systems spanning sites; site comparison |

## 8) Risks & Open Questions

0. ~~**Where does Charts sit?**~~ Resolved in §13: its own rail item, **Chart**, a sibling of Model.
1. **Arrangement of the Overview:** card grid, control structure, or both? Both
   are mocked.
2. Does `degraded` propagate along `part_of` to the parent always, or only past a
   declared fraction of children?
3. Who owns Control items for site health: a new watch principal per site, or the
   existing data agent?
4. Promotion up the readiness ladder is a gated, reversible, evidence-carrying
   decision. Should it share one mechanism with other graduation decisions on
   the platform?
5. Is the hypothesis an attest entry type (signed, append-only) or a ModSim
   record that attest countersigns?
6. **Risk:** the first site's stream channel publishes nothing today, so v1 can
   say only "unknown" about the channel that feeds the model. The beat is small,
   but it is consumer work.

## 9) Acceptance & Rollout

- The 2026-10-26 hold, replayed, raises `value.held` on the batch channel within
  its max-hold, and lists the predicted hold as a consequence, not a second
  root.
- A bound app's site health equals the site node's own answer (regression test).
- Every surface shows the same snapshot id and evaluation time.
- A product's specialization is expressed entirely through contributions, and
  passes the conformance suite.
- Rollout is behind a feature flag on the first site, then to the other sites
  in the scope.

## 10) Contacts & Links

- Spec: [`spec-site-view.md`](../specs/spec-site-view.md)
- Mockups: the Site section of the master design, https://claude.ai/artifact/4Wtqj1voZMCdw56C24HHLv (private; the owner
  shares it; a convenience view that may lag, not the source), including the animated Physical mode. Coding agents cannot open it;
  they read appkit's design entry, [`docs/design/site-view/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/site-view), which holds
  the README and the platform icons.
- Related: ADR-209 (site), ADR-050 (tenant and site), ADR-211 (Control),
  ADR-214 (chart spec, 3D), `spec-modsim-engine.md` (draft), `spec-attestation.md`,
  `spec-axiom-schedule.md`, `spec-data-table.md`
- Prior art: N. Leveson, *Engineering a Safer World* (2011) and the *STPA
  Handbook* (2018); ANSI/ISA-101 HMI and *The High Performance HMI Handbook*
  (2008); ANSI/ISA-95 equipment hierarchy; the Asset Administration Shell
  (Industry 4.0); mimic diagrams in SCADA practice; model discrepancy in the
  calibration literature (Kennedy and O'Hagan, 2001)

## 11) v2: archetypes, disclosure, gravity, layout, app context

*Added 2026-10-29 after the owner's review of the v1 gallery.*

### 11.1 Design from the day: archetypes

People do not visit a "site view"; they come in to do a job, in an order. Each
archetype below is generic, with how it reads at three kinds of site.

| Archetype | Process plant | Farm | Factory | The day, and the questions in order |
|---|---|---|---|---|
| **Operator** | licensed operator starting a shift | grower or farm manager at dawn | line operator at shift start | *What changed since last shift? Is anything abnormal now? Can I trust what I see? What is planned today?* → watch → hand over |
| **Researcher** | researcher or experimenter running a sequence | agronomist running a trial | process engineer running an experiment | *Is the facility in the state my run needs? Is my data landing? How does predicted compare to measured?* → run → pull data → compare → write up |
| **Maintenance or service** | instrument technician | equipment or probe service | maintenance tech | *What is broken or due, where is it, what is its history, who do I tell when done?* → take offline → fix → verify → sign off |
| **Manager** | site manager | operation owner, advisor across growers | plant manager | *Are we healthy across everything? What is overdue? What needs my decision?* → decide → delegate |
| **Compliance, safety or licensing** | licensing or health-physics officer; regulator (lateral, read-only) | certification or agreement owner | EHS officer, auditor | *Are we within our authorizations? What surveillance or renewal is due? Can I prove it?* → attest → report |
| **Data and model engineer** | modeller, data steward | model owner | process-data engineer | *Are pipelines and models healthy? Is error going down? What should I try next?* → diagnose → build → run → promote |
| **Visitor or student** | student, observer | a guest on the farm | a tour visitor | *What am I looking at? What is it doing right now?* → explore, read-only |

**What changes by archetype.** Not what a person *may* do (that is FGA), but
**order and emphasis**:

- the order of rail items and their clustering;
- what the first glance shows (the "now" strip at the top of the gravity view);
- the default selection and the default bottom-panel tab;
- which layers are on in the gravity view (the operator sees health and flow; the researcher sees the run and its channels; maintenance sees what is due, by location).

**The defaults:**
- **Role-driven, never hiding.** Everything a person is permitted to use is
  present. A role reorders and emphasizes; it never removes. Absence comes only
  from authorization (FGA), as it does today.
- **Roles come from the platform.** Site roles are ADR-209 FGA relations,
  populated by role packs (a base pack plus a site-class pack, as the first
  domain product already declares). Each role in a pack declares a **lens**: rail order, the
  first-glance cards, the default layers and the default bottom tab.
- **Context can pick the lens.** Starting a shift (a recorded shift handover),
  or opening a run, switches to the matching lens for that session. That is the
  only automatic switch.
- **Dual roles.** A person holding two roles gets the **union of permissions**
  and one **primary lens**: the role they last chose, or context's choice. The
  second role's items follow in the rail, not hidden. A lens switch in the
  account menu reads "viewing as Operator · also Researcher" and is sticky per
  person and site.
- **Not more dynamic than that.** There is no learned reordering, and no layout
  that moves on its own. A person may pin items (phase 3). Predictability beats
  cleverness for people who work by muscle memory.

### 11.2 Progressive disclosure

The v1 gallery shows everything at once. v2 discloses in three levels, and the
first level is never prose:

| Level | What it shows | Example |
|---|---|---|
| **1. Glyph** | a status glyph, a count, an icon; one word at most | ● 2 broken · ◐ 1 degraded · ? 1 unknown |
| **2. Line** | one sentence, on hover or a single click | "All 11 measured channels held one value for 3.4 days" |
| **3. Detail** | reasons, evidence, next step, lineage | in the right panel, or expanded in place |

Rules:
- The overview above the fold carries **no paragraphs**: the gravity graphic,
  glyph rows and numbers.
- The roll-up is a row of glyph chips, one per root cause; consequences are a
  count on the chip.
- System cards show icon, name, glyph and one number.
- Lineage shows a chain of glyphs (served → gold → silver → bronze → relay →
  acquisition → instrument). Each node opens to its line, then its detail.
- Density stays where it is expected: **Data mode** tables, logs, and the detail
  panel.
- Captions and explanations move to tooltips and an info glyph (ⓘ).

### 11.3 Gravity: one central noun per product

Every product has one or two nouns that anchor both the interface and the
person's sense of place. The Site view's center is that **gravity object**,
rendered physically. Everything else orbits it: health layers on it, the right
panel describes what is selected on it, the bottom panel gives its history.

| Product | Gravity | Rendered as | Orbiting it |
|---|---|---|---|
| Process plant | **the process unit** (for example a vessel) | the unit in 3D, or its 2D layout map, with instruments pinned | channels, the surrogate's predictions beside measured values, pipelines, the shift |
| Farm | **the field** | the field map with its zones | zones, samples, imagery, the crop cycle, prescriptions |
| Factory | **the line** (several lines: **the floor**) | the line as a P&ID; a floor plan for several | stations, tags, work orders, the shift's production run |

A product declares its gravity: `[site_gravity]` names a member kind and the
representation that draws it (spec §3.2). Without a declaration, the center
is the systems grid, the v1 behaviour.

### 11.4 Layout: left, center, right, bottom

For left-to-right locales; mirrored for right-to-left, using logical CSS
properties so the mirror is automatic.

| Region | Role | Behaviour |
|---|---|---|
| **Left** | orientation and navigation | the scope selector, the rail clustered by lens, the status chip |
| **Center** | the gravity view | the process unit, field or line, with layers. Selecting on it drives right and bottom |
| **Right** | **what** is selected | a header glyph, name and state, then the key values. Then sections that are collapsed to one line each: health, lineage, linked documents, controls, loops. Opens on selection, closes with Escape, is resizable, and is linked by `?sel=` |
| **Bottom** | **when** for the selection | collapsed to a one-line strip with the latest event. It expands to tabs: timeline, logs, history, runs. It is filtered to the selection, or to the site when nothing is selected |

**Ported from an earlier product's field panel** (its map-plus-right-panel layout):
- **Collapse, don't close.** The panel collapses to a narrow rail: a status
  dot that reopens it, and the selection's name, rotated.
- **Resizing.** Drag from the inner edge, with a minimum width and a clamp that
  keeps the center usable. The width is remembered per person and exposed as a
  CSS variable so floating controls stay clear of the panel.
- **Tabs and disclosures.** Tabs per kind of selected object (the farm's field
  panel has Soil, Seed, Crop and Tasks). Inside a tab, a disclosure shows a
  summary or glance first and the detail on expand. A disclosure can pin a
  value that survives collapse, and has a footer rail: provenance on one side,
  *Discuss →* (open in chat) on the other.
- **Severity-first glance.** One verdict with a tone dot that names the binding
  constraint, before any table.
- **Layered Escape.** Escape first undoes an in-progress edit, then clears the
  selection, and leaves the panel open.
- **Phones.** The panel force-collapses and opens as a full-width sheet.

Two changes from the original:
- the whole selection, down to a channel or zone, lives in the URL (`?sel=`).
  The original keeps only the field in the URL;
- a **bottom panel** is added. The original has none.

**How right and bottom combine:** right answers *what is this*, bottom answers
*what happened to it*. They never repeat each other. On narrow screens, the right
panel becomes a sheet over the center, and the bottom panel becomes a tab inside
that sheet.

### 11.5 The Site within the whole app

> **Superseded in part by §12 (v3).** The rail clusters and their headers below are replaced by the standard rail; how a selection carries, Model with Build, and the control model still hold.

The rail, clustered (the order shown is the default lens):

| Cluster | Items | Relation to the Site's selection |
|---|---|---|
| **Place** | **Site** (home: the gravity view) | defines the selection, `?sel=site/system/member` |
| **Observe** | Charts · Logs · Library | Charts open with the selection's channels. Logs filter to the selection (the bottom panel is the same view, inline). Library opens the documents linked to the selection: procedures, calibration certificates, drawings |
| **Act** | Control · Model | Control lists the controls and agents bound to the selection. Model opens the revisions, decks and runs that touch it |
| **Ask** | Chat | the selection is attached as context |
| **Admin** | Access · Settings | — |

**The selection carries.** A selection chip in the page header follows the
person from page to page, for example `example-site › Temp1 ✕`. Every page that
understands it filters to it; clearing it returns the page to site scope.

**Model is where Build lives.** One rail item, *Model*, holds four tabs:

- **Registry:** model revisions, readiness rung, validity envelopes.
- **Build:** create a thing, then register it. That covers input decks, data
  kits, app kits, extensions and agents. Build is part of Model because every
  build ends as a registered, versioned artifact, and most builds on a site
  are models.
- **Runs:** ModSim run records and verdicts.
- **Improve:** the improvement loop and its hypotheses.

A product with no models can relabel the item *Build*.

**Control is one model for agents and manual controls.** A **control** is
anything that can act. Each control has:

- a **target** (a member);
- an **authority** (an FGA relation and the RACI default);
- a **consequence class** (harm, loss, waste or noise; ADR-134);
- a **mode**: manual, advised or autonomous.

There are three kinds:

- A **knob** is set by a person. Examples: a pipeline's cadence, a model's
  served revision, an alarm threshold, a schedule's pause.
- An **agent** is a knob with judgment. Its dials are autonomy, attention and
  budget, and it carries its run history.
- A **scheduled loop** is a knob on a timer.

Each control on a member also appears in the right panel's Controls section.
**The STPA boundary holds:** controls of the plant itself (drives, pumps,
valves, irrigation, line drives) appear only as **observed** controls, a
read-only mirror of the plant's own state. The platform issues no plant
commands, and safety-related controls stay with the plant. Promoting a model into
a control path is a recorded decision outside this view (ADR-036).

### 11.6 What v2 changes in version 1's build

| Version 1 item | v2 change |
|---|---|
| The rail home | the gravity view for the declared gravity: a 2D layout map, built on the v1 schematic, with 3D in phase 2 |
| Roll-up, cards, lineage | glyph-first, with the line and detail levels |
| The right and bottom panels | built. The bottom panel's first tabs are logs and history for the selection |
| Lenses | built from the role packs: rail order, first glance, default layers |
| Rail clusters and the selection chip | built. Model, with Build inside it, and the unified Control are surfaced in phase 2 |

The mockups for v2 sit beside the v1 shots in the gallery. They show the same
site as an operator at shift start and as a researcher; the glyph-first
overview; the four-region layout for a process unit and for a field; and the full
rail with Model and Control.

## 12) v3: the standard rail and the landing page

*Added 2026-10-29 after the owner's review of v2. Replaces the v2 rail (§11.5).*

### 12.1 The standard rail

There are **no section headers.** The rail is one list, in a fixed order from
top to bottom. This standard order applies to every product:

| # | Item | What it is |
|---|---|---|
| 1 | **Search** | everything the person may see |
| 2 | **Chat** | full-page chat; the default landing (§12.2) |
| 3 | **Site** | the center of gravity, named by its noun (the unit, the field, the line) |
| 4 | **Control** | loops, agents, knobs and dials (§11.5's control model) |
| 5 | **Log** | logs and history, filtered by the Site selection |
| 6 | **Library** | the site's documents and artifacts |
| last | **History** | past chats, at the foot of the rail |

**Treatment, matched to an earlier product's rail:**
- Search is an icon button in the rail header, beside the logo and the
  collapse control.
- Rows are compact: 32px high, 16px icons, an overflow menu on hover.
- A divider separates the list from History. History is collapsible, with
  Starred and Recents.
- The rail can be resized by dragging. Collapsed, it is a 56px icon column in
  the same order.

The account menu at the foot holds Access, Settings and admin. Neither is on
the main list.

The center of gravity is reached **by its noun**: there is no separate Home
item that duplicates it. A product renames items and may insert its own items
between Control and Log. Each name is a label override (spec §3.2):

| Rail | Example |
|---|---|
| Axiom (default) | Search · Chat · Site · Control · Log · Library · History |
| A farm product | Search · Chat · **Fields** · *its own items* · Library · History |
| A process-plant product | Search · Chat · Site · **Operate** · *its own items* · Log · Library · History |

**What a product may rename:** the center-of-gravity item (Site to Fields) and
Control (to Operate). **Library is always "Library"** in every product. A
product's own name for its book (the Library's classification tree, renamed
from the Data Book through the book scheme) appears inside the Library's
sidebar, never on the rail.

**Charts was not placed in v3**; §13 places it as **Chart**. Until it is, every selection
offers **Open in Charts**, and charts open with the selection's channels.

### 12.2 The landing page is configurable, and chat by default

After sign-in a person lands where the product says, and **full-page chat** is
the default, as an earlier product does today. The Site page is one click away on the
rail. The setting is `ui.landing`:

- the product sets the default;
- a site may override it;
- a person may override it for themselves.

Resolution and the setting's shape are in the spec (§8.3).

**The chat page is the as-built chat (§21.3).** On landing, one input sits
front and centre with action cards beneath it; once a conversation starts, the
input moves to the bottom and the conversation scrolls up. In appkit the
landing layout is behind a flag that is off on some products today; turning it
on is the change, not a new chat.

### 12.3 Personas configure the rail; people override

Three layers decide which items a person sees. Each later layer narrows the one
before it:

1. **Available.** The item's feature flag is `on` (ADR-210), and authorization
   (FGA) permits it. A flag at `soon` shows the item as coming. This layer has
   no role targeting, by ADR-210's design.
2. **Shown by default.** Each role in a role pack declares a **rail preset**:
   the items it shows by default. For example:
   - an operator does not see Model by default;
   - a researcher does not see Operate by default.
   A person holding **two roles** sees the **union** of their roles' presets.
3. **The person's own choice.** A person may show any available item, or hide
   any item except Chat and the center of gravity. Their choice is kept per
   site.

**Hidden is not forbidden.** A hidden item is one click away: in the rail's
"more" control, in search, and through any link a selection offers. Only
layer 1 removes an item.

| Role (example) | Default rail |
|---|---|
| Operator | Search · Chat · Site · Control · Log · Library · History |
| Researcher | Search · Chat · Site · Model · Log · Library · History |
| Instrument Technician | Search · Chat · Site · Operate · Log · Library · History |
| Site Manager | the full rail |
| Student, Observer | Search · Chat · Site · Library · History |

### 12.4 Plain words, not cryptic tiles

A tile or chip never shows "r?", "—" or a bare question mark as its value. When
something is unknown, it says so in words: "no model status yet", "no
improvement runs yet", "stream not reporting". A glyph may accompany the words;
it never replaces them.

### 12.5 Operate: a glance before the table

The Control page (which a product may call Operate) leads with a row of glyph-and-number
summaries:
- needs you;
- agents running;
- knobs changed today;
- loops overdue;
- observed plant controls.

The dense table sits below that row.

## 13) v4: Model and Chart

*Added 2026-10-29. This section completes the rail and resolves the open question about where Charts sits.*

### 13.1 The rail, final

Search · Chat · Site · Control · **Model** · **Chart** · Log · Library · History

| Rail | Example |
|---|---|
| Axiom (default) | Search · Chat · Site · Control · Model · Chart · Log · Library · History |
| A process-plant product | Search · Chat · Site · Operate · Model · Chart · *its own item* · Log · Library · History |
| A farm product | Search · Chat · Fields · *its own items* · Model · Chart · Library · History; Model and Chart are optional, behind flags |

**Each item has one job:**
- **Control** (or Operate) is for *running* what exists.
- **Model** is for *building* what predicts.
- **Chart** is for *looking at* data.

A product may rename Model (for example "Twin"); Axiom's default is "Model".
Role presets (§12.3) still decide what shows by default. An operator's
preset leaves Model hidden but reachable.

### 13.2 Model is the builder's item

Model covers the whole lifecycle of building something that predicts. Its tabs
follow that lifecycle, and each tab is a stage of the ModSim engine (draft
#1430: slots, run record, verdict):

| Tab | What a person does | ModSim slot or record |
|---|---|---|
| **Registry** | find or create a model; see its revisions, readiness rung and validity envelope | the model registry |
| **Build** | author or modify the input deck; bind it to its code or codes (multiphysics binds several); build data kits (analyses, alerts, new agent skills or tools) | `create`, `sim` |
| **Runs** | schedule and monitor runs; outputs land as **tagged, versioned datasets** | `run`; the run record |
| **Train** | train the surrogate from a run dataset | `train` |
| **Validate** | check against reference problems, measured data and the validity envelope | `validate`, `measure`; the verdict |
| **Deploy** | promote along the readiness ladder (model, shadow, twin) and deploy | the registry's promotion, recorded |
| **Improve** | residuals, hypotheses, targeted losses (§5.5) | the improvement loop |

**Axiom defines the interfaces, not the methods.** It standardizes how the
pieces compose into a twin:
- a model reference and its revisions;
- a deck bound to codes;
- a run record and its dataset;
- a trained surrogate with its envelope;
- a verdict;
- a promotion.

The training method belongs to the product: dynamic mode decomposition or a
reduced-order model for a process plant, a learned model or neural network for a farm
field. Every built artifact (a deck, a dataset, a surrogate, a data kit, a
notebook) is registered and versioned in the same way.

### 13.3 Notebooks, reimagined for agents

A notebook is Model's working surface: a sequence of **cards**, each one of two
kinds, freely mixed:

- **Agent card.** The person writes in natural language, and the card resolves
  it to a known tool. The card shows the resolved call (tool, arguments) before
  it runs. It then executes deterministically and records the call and its
  result. A request that does not resolve to a tool says so; it never
  improvises.
- **Python card.** Code with its output, run in the notebook's kernel against
  the same datasets.

A notebook is **replayable**: its cards re-run in order against recorded
inputs. It is **versioned** and **registered** like any built artifact. This is
how people will drive a simulation. The deck, run and plot workflow that people
do in a general-purpose notebook today becomes a few agent cards and a Python
card, with every step traceable.

The mockup shows four cards:
1. an agent card that tweaks a parameter in the input deck;
2. an agent card that runs the deck;
3. a Python card that plots the result;
4. an agent card that registers the dataset.

### 13.4 Chart is a sibling of Model

Chart is where people look at data. It has three faces of one query:

- **Charts.** Any ChartSpec 2.0 chart, including 3D.
- **Tables.** The platform data table.
- **Queries.** Written in natural language, or as SQL pasted in Chart or in
  Chat. A query runs against the right gold table, with its source, tier and
  row count shown.

**Chart and Model feed each other:**
- a run's dataset opens in Chart;
- a chart's query can be saved as a data kit in Model;
- a selection anywhere offers **Open in Chart**.

This resolves open question 0 of §8: Charts is its own rail item, named
**Chart**.

**Chart is the as-built appkit Charts page (§21.2)**, not a new design: groups
of graphs, the window selector, + Add graph, Share link, Layout, wall-display
mode and the per-graph Configure panel. Additions in this PRD are marked NEW
and layer onto that page.

## 14) v5: structure and behaviour, drill-downs, links, who sees what, journeys

*Added 2026-10-30 after the owner's review of v4. The gallery's v5 section has
every screen below in light and dark. Values from the bound application are
tagged real; logs, workflow records and work orders are labelled fixtures,
because the bound application serves none yet.*

### 14.1 Site and Control: structure against behaviour (decided first)

The v1 to v4 mockups blend two topics. v5 separates them by what changes and
how:

| | **Structure** | **Behaviour** |
|---|---|---|
| What it is | systems, assets, instruments, topology, data paths, configuration, physical layout, lineage *paths* | live state, health, alarms and the attention queue, running loops, agents, knobs and dials, runs, overrides |
| How it changes | rarely, by a configuration change, which is recorded | constantly |
| Its history | configuration history (versions) | the timeline |

**Where v1 to v4 blend them:**

| Screen | Blend |
|---|---|
| v1 and v2 overview ("Systems" mode) | leads with "Broken · held 3.4 days", which is behaviour, on a page about what the site is |
| v2 to v4 overview tiles | structure counts ("11 channels") sit beside live states ("26 channels held") in one grid |
| v2 to v4 Site, operator lens | the "now" strip and the health layer sit on the structural core map, with no mode saying which is which |
| v2 to v4 Site right panel | a Controls disclosure (knobs and agents) and a Loops tab inside an instrument's panel |
| v2 to v4 lineage chain | the path, which is structure, is drawn with live health glyphs on every hop |
| v3 and v4 Control | its selection chip names a structural noun ("site › pipelines"), and the observed plant controls are structure |
| v4 researcher panel | "Facility state" checks (behaviour) under the run's structural facts |

**The hard case is the center of gravity.** The core, field or line is a
geometry, and geometry is structure. But operators want it *live*. v5 treats
the geometry as a **canvas**, not a topic. Each mode paints its own meaning on
the same canvas:
- the live mode paints state, occupancy and attention;
- the structure mode paints positions, declared elements and connections, with
  no state colour;
- the automate mode paints what acts on each element.

The selection stays when the mode changes.

**Three options, tested on operator shift start and the scorecard (§14.7):**

| | A. Truly distinct items | B. One item, "Control" | C. One item named for the center of gravity |
|---|---|---|---|
| Rail | Site (structure) and Control (behaviour, with the live mimic) | Control, with sub-areas Site · Operate · Automate | Site (or the product's noun), with modes Operate · System · Automate |
| Operator shift start (clicks, 7 steps) | 6; "can I trust what I see?" needs two items (fails H6) | 5 | 5 |
| ≤2 clicks to anything (H1) | passes | passes | passes |
| Back in one click (H2) | the trail crosses items on every structure question | passes | passes; a mode is part of the restored state |
| Empty state | two empty items for one unconfigured site | System and Automate empty under "Control" | one item: System shows the setup steps; Operate says nothing is reporting |
| Visitor or student | sees "Site" | sees "Control" on the rail, which reads as authority they do not have | sees "Site", System by default |
| The canvas | drawn in two items | drawn once | drawn once |

**Recommendation: C.** One rail item named for the center of gravity, with
three pure modes:

- **Operate**: state, attention,
  today's plan, live system states. Default for operators, maintenance,
  managers, compliance and researchers.
- **System**: systems with their declared members and connections, data
  paths, configuration and its history. No state colour. The right panel shows
  what an element *is*. Default for data and model engineers, visitors and
  auditors.
- **Automate**: knobs, agents, loops and schedules, and the observed plant
  controls. It is gated as Control was (ADR-211).

**No screen mixes the two.** Within a mode, every panel is of that mode. Two
things cross modes by rule, not by blending:
- links, which go to the object's home;
- the shell's chrome (the trail and the attention bar with obligation
  countdowns and the tray), which is the same on every screen and is not page
  content.

**Rail consequences.** Control leaves the standard rail and becomes the
Automate mode:

| Rail | v5 |
|---|---|
| Axiom (default) | Search · Chat · Site · Model · Chart · Log · Library · History |
| A process-plant product | Search · Chat · Site · Model · Chart · *its own items* · Log · Library · History |
| A farm product | Search · Chat · Fields · *its own items* · Model · Chart · Library · History |

ADR-211 (Control) keeps its model of loops, agents, knobs and dials. Only its
place on the rail changes; an ADR-211 amendment records that. Role presets
(§12.3) now also choose the default mode.

### 14.2 A drill-down for every rail item

Level 2 is the list or overview in the center. Level 3 is a selected object:
- the right panel shows what it is, its links and its actions;
- the bottom timeline shows what happened to it and its links.

| Item | Level 2 | Level 3 (selected) |
|---|---|---|
| Site › Operate | the canvas with state and the now strip; today's plan; systems with live state | a facility with its reservations and occupancy; an instrument in a declared maintenance window (degraded, not broken) with the consumers it notified; a plan step with its prerequisites and the logbook entry it will write |
| Site › System | the canvas with positions and declared elements; systems with members and connections; data paths | an element's declaration and configuration history |
| Site › Automate | controls (knobs, agents, loops, observed), loops, agents | a knob with its value, authority and the change that would move it |
| Model | registry (readiness rung, verification level, envelope); Validate; Deploy; Improve | a candidate revision: its verdict per check (observed, expected, delta, tolerance), the windows that quality marks excluded, the promotion ask and its approver of record |
| Chart | saved charts, and the chart with marks (workflow windows, held spans, maintenance windows) joined from Log | a window: measured, predicted, residual, quality |
| A product's workflow item | that product's own screens, unchanged (§14.6) | that product's own records, plus the shell's linked chips |
| Log | typed logs (§14.3), each with a header saying who writes, who signs, what is immutable and retention | a signed entry: signer with the authority snapshot, occurred and recorded times, interval, subjects, digest and previous digest, cross-check result, supplements |
| Library | the as-built Library browser (§21.1); the book tree appears as folders | a file with its version history; a record package is a read-only generated folder (NEW) |

### 14.3 Log is one rail item with typed logs, and an Audit mode

| Log | What it holds | Who writes | Who signs | Immutable | Retention |
|---|---|---|---|---|---|
| **Operations** (a product names it, for example Ops) | the attested operations logbook (ADR-142, ADR-150) | operations roles; agents only draft for a named person | the author; co-signature, relief and seal per the logbook's declaration | every record; corrections are supplements, retractions or reclassifications | the logbook's `retention` (site policy) |
| **Workflow** (for example Experiment) | a product workflow's events and approvals | the workflow (events) and the roles acting on it | per the workflow's logbook (for example approvals, custody, disposal) | events are append-only; approvals are attestations | the record's life plus site policy |
| **Maintenance** | work orders, calibration, return to service | technicians | performed and verified by two people | signed steps; the order closes and is never edited | the asset's life |
| **Change** | configuration, deploys, knob changes, model promotions, release notes | the platform, for a person or an agent (audit, change records, release notes) | nobody; approvals are recorded RACI dispositions (ADR-045) | hash-chained per node; an intent line before acting, an outcome line after (ADR-197) | platform policy |
| **System** | platform events, health raises, signals | the platform and agents | never | append-only | rolling |
| **Data quality** | quality marks on readings and windows (ADR-132: good, suspect, bad, saturated, stale) | rules, agents and stewards; work orders and calibration raise marks | a steward's manual mark is a signed act; a rule's mark is a system fact | a mark ends with a new record; the reading's event is kept | with the data |
| **Inspections** | signed reviews and findings (the inverted audit, spec-attestation) | auditors under a grant | the auditor | append-only; access to it is audited | per inspection programme |

Entries link to Site objects through the logbook's `subjects` (sample,
equipment, channel, action, dataset). A work order's quality mark carries the
work order's attestation id in `quality_reason`, which closes the gap noted in
ADR-132.

**Audit is a mode of Log, not a rail item.** Log already spans every log, and
the auditor's preset opens Log in Audit mode. That puts reconstruction 2 clicks
from landing (Log, then a period) without adding a non-standard rail item.
§14.6 describes the mode.

### 14.4 How the pieces link

The gallery's map draws the edges below. Each is a link a person can click: a
chip in the right panel goes to the object's home, which is a rail item, mode
and tab.

| from ↓ · to → | Site | Model | Chart | Workflow item | Log | Library |
|---|---|---|---|---|---|---|
| **Site** | | rung shown on the models system | Open in Chart | position availability | work orders; health raises | |
| **Model** | served-revision knob (Automate) | | | predictors used by the workflow | Change entry | |
| **Chart** | | residual → hypothesis; query → data kit | | | | |
| **Workflow item** | reserves a position; occupancy updates the configuration; adds a plan step (Operate) | predicted values; context for the twin | marks the window | | workflow entries; drafts an operations entry | generates the record package (a read-only folder, §21.1) |
| **Log** | operations facts (interval, configuration) | quality marks exclude slices | quality marks shade data | a signed operations entry completes a workflow transition | | exports; certificates |
| **Library** | documents on every object | | | | | |

**Three flows** are clickable in the gallery. Each step keeps the selection,
shows the trail and names the next rail item.

1. **A workflow request, end to end.** The request reserves a position on the
   canvas, adds a plan step and writes operations and workflow entries. It also
   marks a window in Chart, feeds a residual into Model's Improve, and
   generates the record package as a read-only folder in the Library (§21.1).
2. **A work order takes an instrument offline.** The work order sets a
   maintenance window (Site, Operate). It marks the instrument's readings bad with
   reason `maintenance.offline`, and the improvement loop excludes those
   slices. It also holds the plan's checks until the instrument returns to
   service, and writes an operations entry plus a supplement on return.
3. **A residual becomes a promoted model.** The path runs:
   - the residual, in Chart;
   - the verdict, with quality-excluded windows (Model, Validate);
   - the promotion ask, to the approver of record (Model, Deploy);
   - the served-revision knob (Site, Automate);
   - a Change entry.

### 14.5 Who sees what

Each rail item, typed log and key action takes one of five states for an
archetype:

| State | Meaning | How it renders |
|---|---|---|
| **shown** | permitted, and in the role's preset | normal |
| **read-only** | visible, but the role cannot act | actions render with the role that takes them ("an approver signs; never the requester") |
| **hidden by default** | permitted, but not in the preset | under More, one click |
| **linked only** | the role sees only entries linked to their own objects | a count of the others, not their content |
| **not available** | FGA or a flag removes it | absent from the rail, search and links; a stray link lands on a page that says why and offers a next step |

Only "not available" removes anything (§12.3). The archetypes are §11.1's plus
**Auditor**:

| Archetype | Site | Automate | Model | Chart | Workflow | Log | Library |
|---|---|---|---|---|---|---|---|
| Operator | shown (Operate) | shown | hidden by default, read-only | shown | shown | shown; signs operations | shown |
| Researcher or experimenter | read-only | hidden by default, read-only | shown | shown | shown; requests, never approves | operations entries linked to their own objects only | shown |
| Maintenance | shown | shown (providers) | hidden by default, read-only | shown | hidden by default, read-only | shown; writes Maintenance | shown |
| Manager | shown | shown | shown; approver of record for promotions | shown | shown | read; exports and verifies | shown |
| Compliance or safety | read-only | hidden by default, read-only | hidden by default, read-only | shown | shown (its own approvals) | shown; signs its own entry types; Audit mode | shown |
| Data and model engineer | read-only (System) | shown (data and model knobs) | shown | shown | hidden by default, read-only | Change and Data quality write; operations hidden by default | shown |
| Visitor or student | read-only, explanations on | not available | not available | hidden by default, curated | not available | not available | public tier |
| Auditor | read-only (System) | read-only | read-only | read-only | read-only | Audit mode; writes only Inspections | read-only |

**Key actions** (gallery: "Who sees what"):
- sign an operations entry;
- co-sign or seal;
- propose or approve a workflow request;
- execute a transition;
- approve a disposal;
- open a work order;
- mark quality;
- ask for or approve a promotion;
- change a knob;
- export evidence or verify the chain;
- see predictions;
- see controlled detail.

A licence or qualification is an attribute checked at signing, and it is
snapshotted into the record. Platform administrators never sign (ADR-142).

**The same screen as two archetypes.** The gallery mocks three pairs, with
hidden items drawn as ⊘ ghosts for review:
- an operations entry, as Operator and as Researcher;
- a pending workflow request, as approver and as requester;
- the canvas, as Operator and as Visitor.

### 14.6 Compliance and the auditor function (general Axiom capabilities)

**Compliance is a system of the site.** Its members are obligations:
- licences and permits;
- surveillance or inspection requirements;
- attestations due (attest `[[obligation]]`).

Each obligation has:
- a state;
- evidence links (the signed entry, the certificate in the Library);
- its compliance loop: **due → performed → attested → verified**.

The verify step is a reviewer's signed review in the inspections logbook.

| State | Source |
|---|---|
| met | attest `ok`, plus the signed entry that met it |
| due | attest `warn` (within its lead or grace) |
| missed | attest `missed`, a platform fact that is never erased; a later entry records `met` and the miss keeps its duration |
| waived, with authority | **proposed, not in attest today**: a signed record by the role with authority, with a reason and an end |

The obligations are declared in System, and their states are in Operate.

**The auditor archetype** has these properties:
- **Access:** read-only and cross-cutting, under a grant that is time-boxed and
  expired by KEEP. The access is itself conspicuously audited.
- **Signing:** signs nothing operational; writes only reviews and findings.
- **Corrections:** never edits. A correction is a new attested entry by an
  authorized role.

**Audit mode (inside Log)** has five tabs:
- **Reconstruct.** Pick a period and an object, or come from an incident chip.
  You get one timeline across every log, approvals, changes and lineage, with a
  per-source **chain status**:
  - attest logbooks: verified chain and anchor (`axi log <log> verify`);
  - the actions ledger: "HMAC chain intact" (`axi audit actions --verify`);
  - GUARD verdicts: a provenance chain, labelled *not cryptographic*, with
    `chain`, `causes` and `explain`;
  - change records: intent and outcome paired;
  - lineage: content hashes.
- **Trail.** Search by who, what, when and object, with `explain` on any
  verdict.
- **Evidence.** Export a **signed, self-verifying package**: entries,
  attestations, approvals, change records and verdicts, lineage paths, data
  snapshots with hashes, anchors, and the verifier. The export is recorded.
  This matches spec-attestation's evidence packages, which are designed but not
  built.
- **Findings.** A loop: observation → finding → corrective action (owner, due
  date) → verified by the auditor → closed. The owner closes the action; the
  auditor verifies it.
- **Sampling.** Draw N entries from a population with a recorded seed, then
  check them: the signer held the role and an active qualification at signing,
  had a fresh sign-in where required, and the cross-check result is present.

**Placement.** Audit is a mode of Log (§14.3). Compliance also appears as a
system on the Site, with live states in Operate and declarations in System.

**Built against designed:**
- **Built:** attest chains, anchors, obligations (ok, warn, missed) and verify;
  GUARD's verdict trail (`list`, `show`, `chain`, `causes`, `explain`); the
  actions ledger's HMAC verify; KEEP expiry.
- **Designed only:** the reviews logbook and its coverage, evidence packages,
  and the anchor check in the verifier.
- **New in this design:** waivers, findings with corrective actions, and
  sampling.
- **WARDEN** is a federation gatekeeper and arbiter, not the auditor. The
  auditor function is not given to it.

### 14.7 Journeys and design-quality tests

**Heuristics:**

| # | Heuristic | How it is met |
|---|---|---|
| H1 | ≤ 2 clicks to anything from anywhere | rail item + object; a linked chip; or ⌘K + pick |
| H2 | back in one click | the trail restores rail item, mode, selection, filters, view mode and time window |
| H3 | the most frequent task ≤ 1 click from landing | the landing brief, the attention bar (obligation countdowns) or the tray (drafts to confirm) |
| H4 | no dead ends | every result names its next step; a "not available" page explains why and offers a next step |
| H5 | the selection is carried across rail items | the chip travels; the next item opens on it |
| H6 | one screen per step | detail opens in the right panel, not a new page |
| H7 | never re-key what the system knows | drafts come pre-filled from events; ids are picked, not typed |

**Scorecard, v4 → v5** (P pass, F fail; the number is the worst step's
clicks, or the count of failing steps):

| | Operator | Researcher | Maintenance | Manager | Compliance | Data and model | Visitor | Auditor |
|---|---|---|---|---|---|---|---|---|
| H1 | F 4 → P 1 | F 3 → P 2 | F 3 → P 1 | F 3 → P 2 | F 4 → P 2 | F 4 → P 1 | P → P | F 4 → P 2 |
| H2 | F → P | F → P | F → P | F → P | F → P | F → P | F → P | F → P |
| H3 | F 4 → P 1 | F 2 → P 1 | F 2 → P 1 | F 3 → P 1 | F 3 → P 1 | F 2 → P 1 | P → P | F 4 → **F 2** |
| H4 | P → P | F 1 → P | F 1 → P | P → P | P → P | F 1 → P | F 1 → P | F 2 → P |
| H5 | P → P | F 2 → P | P → P | P → P | F 1 → P | F 2 → P | P → P | F 1 → P |
| H6 | F 1 → P | P → P | P → P | P → P | F 1 → P | P → P | P → P | F 1 → P |
| H7 | F 2 → P | P → P | F 1 → P | P → P | P → P | P → P | P → P | P → P |

**Total clicks per journey, v4 → v5:**

| Journey | v4 | v5 |
|---|---|---|
| Operator | 15 | 5 |
| Researcher | 11 | 5 |
| Maintenance | 11 | 4 |
| Manager | 10 | 5 |
| Compliance | 15 | 7 |
| Data and model | 11 | 5 |
| Visitor | 2 | 2 |
| Auditor | 18 | 5 |

The gallery's Journeys page lists every step.

**Fixes that won the counts:**
- **Attention bar.** It appears on every screen. The operator's obligation
  countdown ("console check due in 4 min · Sign") opens the draft in the right
  panel.
- **Tray.** Drafts that an event pre-filled for a named person.
- **Today in Operate.** The day's plan, with the logbook entry each step writes.
- **Preset defaults by role:**
  - default mode: Operate, or System for engineers, visitors and auditors;
  - default tab: Model opens on Improve for the model owner, and Log opens in
    Audit mode for auditors;
  - default selection: the worst due item for maintenance.
- **Chips that carry the selection.** For example, from a chart window to
  Improve.
- **"Not available" page.** It replaces failing quietly.

**Still failing, by decision:**
- **Auditor H3.** Reconstruction is 2 clicks, not 1. A persona-only rail item
  would make it 1, but would break the standard rail.
- **Researcher H6** now passes through the shell shortcut (§14.12, decision 5).

**Breadcrumbs: the trail.**
- **Content.** A compact trail of up to four prior states sits at the top of
  the center pane. Each crumb is a rail item, a mode, a selection and a tab,
  plus the "now" crumb. A crumb restores the full state: the selection,
  filters, view mode and time window.
- **Back.** ← (the first crumb), Alt+←, or `[` steps back one state. The
  browser's back button does the same, because every state is a URL.
- **Scope.** The trail is per tab and per session. Inside a flow, it is the
  flow's steps.
- **Inside an item.** A product item's own breadcrumb, such as
  "Item / Queue / id", sits inside the right panel and is unchanged.

### 14.8 Empty states and first run

**A new person** with no charts, saved views or data kits sees starter
content, labelled as starter:

| Item | What a new person sees |
|---|---|
| Chart | the site's default chart groups, from site configuration |
| Model | a sample notebook against a public reference problem |
| Chat | suggested questions |
| Site | the canvas with the site's own state |

Every empty state has one obvious next action. Nothing pretends to be data.

**An unconfigured node** shows the first-run setup:
1. bind a site;
2. declare instruments;
3. connect a source;
4. storage (the default local database is enough);
5. invite people from role packs;
6. a model (optional).

Chat can drive every step. Meanwhile each item degrades in its own way:

| Item | While the node is unconfigured |
|---|---|
| Site, System | the skeleton, with each system saying what it needs |
| Site, Operate | "nothing reporting", never "healthy" |
| Site, Automate | agents and knobs only |
| Model | the sample notebook |
| Chart | offers to connect a source |
| Product items | behind their flags until their prerequisites are declared |
| Log | System and Change only, until a logbook pack is installed |
| Library | uploads only |

The gallery mocks an empty Site, an empty Chart, an empty Model and the
setup screen.

### 14.9 Scale: where it breaks

| Piece | Breaks at | Design answer |
|---|---|---|
| Scope selector | ~15 sites as a flat list | type-to-filter and ⌘K; pinned and recent first; "needs attention first"; grouped by organization; roll-up counts; virtualized above 200 (mocked with 50) |
| The canvas | ~300 marks | level of detail by the skeleton: one bubble per ring, area or system, with the count, the worst state and the not-ok count; exceptions listed in the panel; positions render only when zoomed (mocked with 1,524 channels) |
| Chart and tables | 10⁹-row tables | queries over gold aggregates (time buckets) by default; the row count is shown before running; raw reads paged and server-side; downsampling declared on the chart |
| Log | thousands of entries a day | typed logs; filtered by the selection by default; grouped by interval (a run, a shift); a sealed interval collapses to its summary; server-side search |
| Loops | hundreds of loops | grouped by kind and system; "overdue" and "needs you" first; one row per loop *kind* with counts |
| Lineage | deep graphs | show the path for one value, never the whole graph; collapse identical hops; expand on demand |
| Runs | many concurrent runs | the plan shows runs as lanes; "runs in progress" is a count with drill-down |
| Roles | many personas | presets compose by union; the rail never grows past the standard items plus the product's own; More holds the rest |
| Slow links at a partner site | high latency or intermittent links | the shell and last-known state are cached and labelled "may not show the newest"; drafts are kept locally until the node is reachable; signing waits for the node and never fakes "signed" |

### 14.10 Mobile, later

Nothing here blocks a phone layout:
- **The rail** becomes a bottom tab bar of the role's first four items, plus
  More.
- **The right panel** becomes a bottom sheet over the canvas.
- **The center of gravity** stays the screen.
- **The trail** collapses to a back control.
- **The attention bar** becomes a single badge.
- **The modes** become a segmented control.

The device model and its mockups are §18 (v11).

### 14.11 Decisions (owner, 2026-10-30)

1. **Site and Control are combined.** There is one rail item, **Site** (a farm
   product: Field), with three modes. Each mode is strictly one thing:
   - **Operate**: what is live and changing.
   - **System**: static configuration and architecture.
   - **Automate**: agents, loops, knobs and dials.

   "System" replaces "Structure". Inside System, the view modes of §5.6 are
   renamed **Physical · Diagram · Data**, so that only one thing is called
   "System".
2. **The rail:** Chat · Site · Model · Chart · *the product's items* ·
   Log · Library · Team · History (Search moved out of the rail on 2026-10-30; see §21). There is no Control item. **ADR-211 needs an
   amendment.** Its model of loops, agents, knobs and dials is kept; it lives
   as Site's Automate mode, not as a rail item.
3. **Landing.** The Axiom default is full-page chat. Persona presets set their
   own landing, and a person may override it:

   | Persona | Lands on |
   |---|---|
   | Operator | Site › Operate |
   | Researcher | Chat, or the product's workflow item ("my experiments") |
   | Maintenance | Site › Operate, filtered to work orders |
   | Auditor | Log › Audit |

   The configuration contract is in spec §8.3 and its v5 addendum.

### 14.12 Decided (owner, 2026-10-30; formerly open questions)

1. **Waivers.** A waiver is a **separate signed record**, not an attest state.
   - **Contents:** who, the authority, the reason, the scope and an expiry. It
     is revocable.
   - **Effect:** it suppresses the obligation's alarm. The obligation links to
     it and displays "waived until … by …: reason".
   - **Not applicable by mode:** this is recorded automatically and cites the
     operating mode (for example, an obligation that holds only while an
     interval is open). Routine mode cases need no signature.
   - **Missed and waived stay distinct**, and auditors can list and sample
     waivers.
2. **Findings and sampling** are built **inside attest**. They comprise:
   - the reviews logbook;
   - findings with the corrective-action loop (observation → finding →
     corrective action with owner and due date → verified → closed);
   - seeded, reproducible sampling.

   They share attest's signing, chains and evidence packages.
3. **Retention.** The platform defines **retention classes** with enforced
   floors:

   | Class | Floor |
   |---|---|
   | system | about 1 year |
   | operational | ≥ 5 years |
   | qualification | ≥ the current cycle + 1 |
   | lifetime | the life of the site or its licence |
   | legal hold | until released |

   Each log type declares its class, and a product maps its logs to classes.
   A site can only lengthen a floor, and every deletion is attested.
4. **Data and model engineers read the operations log by default,**
   read-only.
5. **A product's frozen workflow against the heuristics** is resolved with
   **shell shortcuts only**. No frozen screen, field or rule changes:
   - after a request, the shell offers "Book the slot →", which opens the
     product's own booking screen pre-filled;
   - pending approvals that belong to the person (for example disposals)
     appear in their attention bar and tray, and open the product's own
     approval screen directly.

   In §14.7, Researcher H6 now passes (one action from the request to the
   booking), and Compliance's disposal step drops to 1 click.
6. **The attention bar** is kept on every screen, System included. It is shell
   chrome:
   - it is scoped to the person's own obligations that are due soon or
     overdue, and their own drafts;
   - it takes one line, and is absent when there is nothing;
   - it is configurable per person and per persona: on or off, which item
     kinds, and the "due soon" window (spec, configuration contract).

Remaining work, not a question: the ADR-211 amendment (§14.11).

## 15) v6: ConCert, continuous certification (the inverted audit)

*Added 2026-10-30. The gallery's v6 section shows the mockups. ConCert
inverts the traditional audit (a tour, findings, a report much later, and
anxiety throughout).*

**A site seeks certification.** It aims for continuous, scheduled,
low-friction sign-offs:
- The system gives the auditor everything needed to sign off, remotely where
  possible, including visual inspection.
- On-site visits are supported when they cannot be avoided.

**It builds on what attest already has or is approved to build:**
- spec-attestation's "continuous review (the inverted audit)": the reviewer
  role, the reviews logbook and `reviews/coverage`;
- findings with the corrective-action loop, and seeded sampling (§14.12);
- signing, chains, anchors and evidence packages.

ConCert is a generic Axiom capability.

### 15.1 Controls and continuous evidence assembly

**A control** is a claim that is certified per period. It is one of:
- an obligation ("console checks met every time they applied");
- a group of obligations;
- a declared property ("the operations log is intact").

**A control declares:**
- its cadence;
- its evidence recipe;
- its control class (§15.6);
- its owner at the site.

**Evidence assembles continuously, never at visit time.** For each control
and period the system gathers:
- the entries and attestations;
- chain and anchor verification;
- a seeded sample drawn when the period opens;
- lineage to the data and its sources;
- waivers and not-applicable facts;
- visual captures.

**Readiness.** Each control is always in one of five readiness states:
1. **ready to certify**;
2. **missing evidence**, naming what is missing, who is capturing it, and when
   it is due;
3. **walkthrough booked**;
4. **finding open**;
5. **certified**, with nothing due.

### 15.2 The auditor's main screen is the ConCert queue

ConCert is a **mode of Log**. When an auditor holds a grant, Log opens on the
queue. Audit's five tools (Reconstruct, Trail, Evidence, Findings, Sampling;
§14.6) sit beside it as tools.

**A queue row** shows:
- the control and its cadence;
- **certified through** (the date);
- readiness;
- **certified by**: a person, or an agent with its model and version;
- the assembled evidence, in one line.

**The evidence view.** Opening a ready item shows its pre-assembled evidence:
- what certifying means: the control, the period and the claim;
- one card per source, with its verification;
- visual captures, each with time, location, device, hash and who.

It offers **Certify · Raise finding · Request evidence**:

| Action | What it records |
|---|---|
| **Certify** | a signed review in the reviews logbook (meaning `verified`), extending "certified through" to the next cadence |
| **Raise finding** | opens the findings loop |
| **Request evidence** | creates a capture task |

Sign-offs are small and frequent: one control for one period. A gallery mock
shows the queue with 8 controls.

### 15.3 Visual inspection, remote first

| Way | How | Evidence |
|---|---|---|
| **Evidence-capture task** | requested by the auditor or by schedule; it appears in the capturer's attention bar (and the tray) | a signed photo or video: time, location (site anchor), enrolled device, content hash, who; the capturer signs it with Sign · Hold · Ask |
| **Live walkthrough** | scheduled; a staff member on site, the auditor remote | the session is recorded and signed per segment, and each checklist item is linked to the timestamps where it was seen |
| **On site** | an offline-capable mobile checklist | items signed on the device, synced when reachable; it never shows "signed" before it is |

The phone layout follows §14.10: the attention bar becomes a badge, the task
fills the screen, and Sign sits within reach of the thumb.

### 15.4 Certification posture

Each control has a standing **certified-through date**, the certifier (a
person, or an agent with its model and version) and the **next small lift**.

The Compliance system on Site shows the posture across controls:
- **Site › Operate** shows the due items and the posture;
- **Site › System** shows the control definitions, cadences and recipes.

The site sees what the auditor sees, so nothing is a surprise.

### 15.5 The ConCert loop

The loop runs: **schedule** (a cadence per control) → **assemble** →
**request missing evidence** → **review** → **certify** or **finding** →
**corrective action** → **re-certify**.

It maps onto the loop contract (spec §3.1):

| Loop field | ConCert |
|---|---|
| kind | compliance |
| cadence | per control |
| steps | as above |
| systems | Compliance, plus the systems the evidence recipe reads |
| health | **broken** for a finding open or a lapsed certification |
| | **degraded** for missing evidence near the due date |
| | **ok** when the control is certified through the current period |

### 15.6 External audit AIs

The design assumes that **external audit AIs may do the checks**.

**The interface.** ConCert's skills are exposed on Axiom's roll-up MCP server
(the single `axiom` server), with parity across CLI, HTTP and web (ADR-056:
skills first, thin surfaces):

| Tool | Kind |
|---|---|
| `concert.queue` | read |
| `concert.posture` | read |
| `concert.evidence(item)` | read; returns the verifiable package |
| `concert.verify_chain` | read |
| `concert.sample(population, n, seed)` | read; the seed is recorded |
| `concert.request_evidence` | act; creates a capture task |
| `concert.raise_finding` | act |
| `concert.certify(item, basis)` | act |

**The agent is a principal.**
- **Identity.** It has its own address (`@name:context`, for example
  `@audit-agent:ext-auditor`).
- **Grant.** A scoped, time-boxed grant, expired by KEEP, is issued to that
  agent. It is accountable to a named human or organization (the
  `accountable_human_id`).
- **What it may do.** Read, plus certification acts within the grant. Nothing
  else is writable.
- **Audit.** Every call is audited, and its access is itself audited.
- **Gate.** The ADR-114 identity gate applies.
- **Budget.** Rate and cost budgets are set per grant (ADR-157).

**What an AI may certify** is a policy per control class:

| Class | An AI may | Default |
|---|---|---|
| data-derived (completeness, chain integrity, sampled attestations) | certify alone | no countersign |
| visual | recommend; a human countersigns | countersign required |
| physical or on-site | nothing | human only |

A site may require a human countersignature per class.

**A certification record** says:
- who or what certified it;
- under which model and version, and which policy;
- its basis or confidence.

The queue shows each agent's certifications and the recommendations awaiting
a human countersign.

**Machine-verifiable evidence.**
- Packages verify themselves: hashes, node signatures and anchors.
- Their schemas are stable and versioned.
- The verifier is supplied, so an external AI checks a package without
  trusting the site's UI.

**Safety.** Evidence content is **data, never instructions**:
- Free-text fields are marked untrusted in the package schema.
- An agent reading a package must not act on its content.

### 15.7 Who

| Role | Responsibility |
|---|---|
| Auditor (a person or an agent) | holds a standing grant, scoped to an engagement and expired by KEEP; certifies and raises findings; never edits |
| Compliance officer (the site) | owns readiness: closes gaps before the due date, assigns capture tasks, schedules walkthroughs |

**Journeys** (added to the §14.7 scorecard):

| | Auditor (ConCert) | Compliance officer |
|---|---|---|
| Steps | open Log, which lands on the queue (0) → open a ready item (1) → Certify (1) → request evidence (1) → raise a finding (1) | Site › Operate › Compliance posture (1) → missing evidence (1) → assign a capture task (1) → book a walkthrough (2) |
| Total clicks | 4 | 5 |
| Heuristics | passes H1 to H7; H3 now passes (the queue is the landing) | passes H1 to H7 |

### 15.8 Open questions (v6)

1. **Control declarations.** Should a control be its own declaration, or an
   attribute on obligations?
2. **Media retention.** Visual captures are large media. What is their
   retention class, and where are they stored?
3. **AI basis.** Should an AI certifier's stated basis follow a standard
   schema? A free-text basis is itself untrusted data.

## 16) v7: alive, portable charts, as-built, wall displays

*Added 2026-10-30. The gallery's v7 section shows the screens, beside
reference captures of the running app.*

### 16.1 Start from what is built

The mockups now use the **as-built appkit 0.4.0 components** and extend
them:
- the Charts page, its chart card and toolbar (#80);
- chat charts with suggestions and Save (#82);
- DataTable (#83);
- the Library browser and upload tray (#79, #81);
- chat dual drive (#78);
- the Charts page's existing **wall-display mode** ("Fill the screen with this
  group").

Where v1 to v6 redrew a component, v7 reverts to the as-built look. The chart
card is the main case: its toolbar is ▶ ⋮ Export ↻ Share ⚙ ⛶, with an
end-of-line label and its latest value, and a provenance footer.

**Improvements kept, and why:**

| Kept | Why |
|---|---|
| marks drawn as bands on the card (a workflow window, a held span, a maintenance window), joined from Log | the as-built card cannot show why a stretch of data looks the way it does |
| "measured" and "simulated" in the legend, simulated dashed | a twin must never be read as a measurement |
| real and fixture tags | review honesty only; not product UI |
| linked chips in the right panel | the interconnection model (§14.4) |
| **Pin to…** in the card toolbar | §16.3 |

### 16.2 Alive and readable at a glance, where it earns its place

| Where | What | Why |
|---|---|---|
| Site › Operate › Systems | a 7-day trend on Instruments (fresh readings per hour), Data pipelines (rows per hour into gold) and the models (the residual, real data) | these three have a quantity whose trend tells the state; the other systems have none, so they get **no** sparkline |
| Site › Operate › Today | a moving progress bar for the open run, and a ticking countdown to the next obligation | something is running; show it running |
| Wall displays | a live chart, the live core, run progress, a countdown, a camera | a wall is read from across a room |
| Hover (§16.6) | a value's mini-trend | a trend on demand, not on every row |
| ConCert queue | the glance row is enough | no chart; a chart would only decorate |
| Log | none | the list is the content |

A chart is never added to fill space.

### 16.3 Charts are portable: chart slots anywhere

- **Pin to…** (NEW) in Chart sends a chart to another place, as a reference,
  not a copy: a Site › Operate panel,
  the run-plan header, Model › Improve, a Log view header, or a wall display.
  A product's frozen screens are not offered (for example, a product's
  workflow item).
- **Add a chart slot.** In any view, an editor adds a slot from the **Charts
  catalog** or builds a chart there. The catalog groups:
  - charts suggested for this context;
  - the site's defaults (from site configuration);
  - saved charts.
- **A slot references a catalog item.** Edits propagate to every slot. A slot
  may override its window, size and title.
- **Hide or turn off per slot, per persona.** An off slot renders nothing: no
  empty frame.

### 16.4 Wall displays (big display mode)

**Expand.** Any status page, chart, schedule, run or Site view can expand to
full screen. This builds on the Charts page's existing wall-display mode.

**Compose.** An editor composes a **wall display**: a grid of slots taken from
the catalog. Slot kinds are:
- charts;
- status tiles;
- the schedule;
- the live center-of-gravity mimic;
- run progress and countdowns;
- the attention list;
- cameras.

A wall display is saved and shared by URL.

**Live.** It updates without anyone touching it:
- auto-refresh;
- motion where something runs;
- large type and high contrast, dark first.

**Kiosk-safe.**
- It runs in a read-only session.
- It shows no personal data by default. What a wall may show is configurable
  per persona.
- It survives network blips: it keeps the last frame and says "showing 13:02 ·
  retrying". It never blanks.

**Measured against simulated (essential for a twin).** Every twin value is
labelled **simulated**, with its model revision, rung and freshness. Measured
values carry their freshness too. Held values say "held".

The gallery mocks two walls:
- a control-room wall for a process plant;
- a farm wall.

### 16.5 Real movement

Where it exists, real movement is preferred over pseudo-movement. **A live
camera** is a slot in Site › System › Physical and on a wall display. It is
access-controlled, labelled LIVE, and shows STALE with an age when frames
stop.

### 16.6 Feel

**Principles:**
- **Hover reveals more, everywhere.** Hovering shows:
  - a value's mini-trend and lineage;
  - a glyph's one-line reason;
  - a tile's sparkline;
  - a chain's verification status.

  Hover never hides what a click would show. Keyboard focus shows the same as
  hover.
- **Infographics invite drill-down.** Every chart, band or bubble opens the
  thing it summarizes.
- **Pseudo-movement only where it explains.** Examples: data flowing along a
  path (held hops pulse instead), a run progressing, a loop cycling.
  Real movement (§16.5) comes first when available.
- **It comes alive as you interact.** Selection and drill-down get subtle
  transitions of 150–250 ms.
- **Optional subtle sounds** for sign, certify, save and alert. They are off by
  default, a person can toggle them, a sound is never the only signal, and
  wall displays keep quiet hours.

**Guardrails:**
- **Performance budget.** Animation is CSS-only (transform and opacity). At
  most one moving element per panel. No animation runs while the tab is
  hidden. A frame takes ≤ 4 ms of script.
- **Reduced motion.** `prefers-reduced-motion` stops all pseudo-movement and
  transitions. State still reads through the word and the glyph.
- **Accessibility.** Status is never shown by colour, motion or sound alone.
  Tooltips are reachable by keyboard and announced.
- **No motion for its own sake.** Every moving element must answer "what is
  moving in the world?"

### 16.7 Open questions (v7)

1. **Catalog ownership.** Is the Charts catalog per site, per person, or both?
   Who may edit a chart that is pinned in other people's views?
2. **Camera feeds.** Retention and access per camera, and whether a camera
   frame may serve as ConCert visual evidence.
3. **Kiosk identity.** Should a kiosk's read-only session be a device
   principal (ADR-114) with its own grant?

## 17) Public site: marketing and intake

*Added 2026-10-30. The mockups are in the gallery (v8, v9). Also decided today:
anonymous guest chat on a node stays closed. v9 (§17.7 to §17.13) adds the
delayed live view, the replay, the audiences, the credit, the public-questions
options and the parity rule.*

A site may have a **public, beautifully curated site** for marketing and
intake. It shows charts and graphs. Its JavaScript provides surface
interactivity only. It is **safe by construction**: there is nothing on it
to break into.

### 17.1 Static and separate

**Where it is served.** The public site is curated HTML, served from a public
edge or CDN. It is **never served from the site node**.
- It has no route to the data door, the API or chat.
- A build step generates it from the published set. A change is a new build;
  nothing is rendered on request.

### 17.2 Charts from published snapshots

**The pipeline.** A chart reaches the public site in four steps:
1. **Select.** A curator picks catalog charts or datasets.
2. **Approve.** Every item passes a **publication approval**:
   - a sensitivity check;
   - an export-control check;
   - the PRESS content gate (redaction and scrub lists);
   - a **data-quality check** (below);
   - an **attested sign-off** by an authorized role.

**The data-quality check** refuses data that carries quality marks or overlaps
a known incident:
- **Marks that refuse:** bad, suspect, held, and stale (sentinel, consistent,
  `stale_source`, and the like). A held value is a stuck snapshot, not a
  reading.
- **Incidents:** data overlapping a known incident window (for example
  "measured channels held") is refused.
- **What the approver sees:** the marks, with counts and the reason. Sign is
  unavailable while any check fails.
- **Picking a window:** when the curator picks, the catalog prefers windows
  whose quality is good, and suggests the nearest clean window.
- **Already public:** an item found to carry held data after publication is
  retired by a signed retirement.
3. **Render.** The approved item is rendered with the platform chart renderer
   (ChartSpec 2.0, scidisplay) into a static chart, with its data inlined.
4. **Interact.** Surface interactivity only: hover, zoom and series toggles,
   computed client-side over the published data. The page makes no network
   calls back.

**What every published chart carries:**
- its source;
- an **as-of time** (a snapshot is never live);
- its approval reference;
- a snapshot hash;
- the quality of its window (good).

### 17.3 Intake: one narrow door

**The forms:**
- contact;
- partnership;
- access request;
- a product's service requests.

They post to one **intake endpoint**:
- It is **write-only**: it only enqueues, and it cannot read anything.
- It is rate-limited and spam-protected. Spam is held, not dropped silently.
- It is deployed apart from the node, and the node pulls from it. Nothing
  reaches inward.

**Triage** is in the app, as **Log › Intake**: a typed log with a queue. A
dedicated inbox is the alternative; the open question below asks which.
- An access request converts to an **invitation** through the gate's existing
  invite flow (a role and an expiry; the person redeems it).
- Privacy:
  - minimal fields;
  - consent text on the form;
  - a retention class: operational, 12 months, unless the request becomes a
    record.

### 17.4 The publishing loop

The publishing loop is **draft → review → approve → publish → retire**. It
satisfies the loop contract: kind publishing, cadence on demand, health
**broken** if anything public lacks an approval.

It reuses the publishing extension and PRESS: draft, render, content gate,
sign and upload through storage backends.
- **Publish history** is attested. Approvals and retirements are signed
  records.
- **Retirement.** Every published item has a retire date, or a signed
  retirement.
- **What's public.** Site › System has a **"What's public"** view: every
  published item, its approval and its retire date. Structure, not live state.

### 17.5 Look

- **Brand.** Curated and brand-driven, using the product's brand tokens.
- **Fast.** Static pages, inlined data, no framework at runtime.
- **Accessible.** Charts have text alternatives and data tables, and status
  is never shown by colour alone.
- **Mobile-first.**

### 17.6 Examples

| Kind of site | The public site |
|---|---|
| A process plant | capabilities, a typical operating day, throughput per month, "request services" |
| A farm | the operation, seasonal yield and soil-health trends (aggregated), "become a customer" or "partner" |
| A factory | product lines, on-time delivery trend, "request a quote" |

### 17.7 The delayed live view: a standing publication approval

*Added 2026-10-30 (v9). Mockups in the gallery (v9).*

A site may publish its own operation, **after a fixed delay**, without a
sign-off per item. The site's policy sets the delay; 24 hours is the
default. A **nightly build** publishes everything that has passed the delay.

**The standing publication approval** is signed once by the site's approver
and names, exactly:
- **which series** (catalog ids, at a revision);
- **which views** may show them (the replay, named charts, key numbers);
- **the minimum delay** (at least the site's policy; the build refuses
  anything newer);
- **the aggregation** (for example 1-minute means), and the retire date.

Under it, the nightly build runs the same checks as §17.2 on every night's
snapshot, automatically:
- the **data-quality check refuses marked data on its own**: held, stale,
  bad or suspect points, or a window overlapping an incident, are not
  published, and the view says so in words (for example "the model's series
  is unavailable for this day: its values were held");
- a refused series never blocks the rest of the day; the build publishes what
  passed;
- every night's publication is an attested record that cites the standing
  approval, with its snapshot hash.

**A manual approval is needed only for a new kind of content**: a series,
view, aggregation or shorter delay that the standing approval does not name.
The standing approval is itself retired by a signed act.

**Still static and separate (§17.1).** The build runs inside the site, pushes
the rendered pages outward to the public edge, and the edge never calls back.
"Live" here means "refreshed nightly", never "fetched on view".

### 17.8 The operating-day replay

The centre of the public site is an animated **replay of the most recent
complete day** that has passed the delay:
- **the physical view** of the main asset, animated over the day (for
  example a glow scaled by a measured quantity, actuator positions), with a
  text alternative and a stated caveat when the animation is illustrative;
- **measured against the model, side by side**, the model labelled as such;
- **events on a timeline**: operations and work completed that day, from the
  site's logs, at the granularity the approval allows (counts and kinds, never
  named people or security-relevant timing);
- **a scrubber**: drag the timeline, play, hold and reset, speeds from 0.5×
  to 20× and a fixed "1 minute per second", a shared cursor across every panel
  and the physical view, a wall-display (full-screen) mode;
- **downloads**: the day's data as CSV, each chart as PNG, and the chart
  spec;
- **a calendar** of published days, marking days with refused series, and a
  list of notable days;
- **key numbers** for the day and since records began, each with its source
  and whether it is measured or derived.

### 17.9 Audiences: funding, recruiting, services

The public site serves three outward goals, configured per site:
- **funding**: public research programs (what the site provides to a
  proposal: access and capability statements, letters, data plans, validation
  benchmarks) and private sponsors (sponsored projects, testing, fellowships,
  acknowledgement);
- **recruiting**: a "study here" or "join us" path for students and
  postdocs (research areas, what people do, outcomes by role, how to apply,
  contact), reachable from the home page in one click;
- **services and intake**: the §17.3 forms.

Each goal is a configurable section and form kind; the home page leads with
the replay and offers the three paths directly beneath it.

### 17.10 A configurable "powered by" credit

A site may show a **"powered by" credit** for the platform or product that
builds it. It is configuration, not chrome:
- placement: the footer (default), and optionally a small line in the
  replay's lineage strip;
- style: muted text at footnote size, never a logo in the header;
- it links to a short "how this site is built" page;
- the program, the organization and its people are credited first;
- off by default for a generic site; a product may turn it on.

### 17.11 Optional public questions (an assistant), off by default

Anonymous chat on the node stays closed (§17 preamble). A public site may
offer questions in one of three modes. **Off is the recommended default**;
the site owner decides.

| Mode | What it is | Risks | Running cost | Guardrails |
|---|---|---|---|---|
| **Off** (default) | no assistant; a FAQ page | none | none | none needed |
| **FAQ-only** | curated, staff-approved answers matched to a question; no model | stale answers | negligible (static) | answers go through the publishing loop; per-IP rate limit |
| **Constrained model** | a model answers from the public corpus only | abuse traffic, cost spikes, wrong or embarrassing answers, prompt injection through the corpus | per question, bounded by the daily budget | public corpus only; no tools, no node access, no intake writes; a bot challenge; per-IP and global rate limits; a hard daily budget that turns it off when spent; cached answers for repeated questions; an abuse kill-switch; every answer labelled as generated, with its sources |

In every mode the assistant is hosted apart from the node, as the intake is,
and nothing it receives reaches the node.

### 17.12 Replacing an existing public site: a parity matrix

When a site already has a public website, the new public site must **not
regress**. Before launch, a **parity matrix** lists every feature of the
existing site (page, navigation item, chart, table, animation, download,
explanation, footer item) with:
- where it lives in the new design: public site, internal app, or both;
- how it improves;
- a status: covered, improved, or gap, then fixed;
- for anything deliberately not carried, the reason, flagged for the owner.

Zero silent drops. The matrix is reviewed with the owner before launch.

### 17.13 Reuse: one codebase

The public site reuses the internal components; it is not a second codebase:

| Public element | Internal component |
|---|---|
| replay panels and every published chart | the platform chart renderer (ChartSpec 2.0), static output with inlined data |
| the animated physical view | the Site › System physical view, driven by the published snapshot |
| key numbers, full-screen replay | wall-display tiles and wall mode (§16) |
| the source, as-of, approval and hash line | the lineage strip |
| data available, notable days, publications, services | the data table |
| forms | the intake (§17.3) |
| the published-days calendar, refusals | the publication records (§17.4) and the data-quality check |

### 17.14 Insiders: sign in, and "Open in the app" (v10)

- The public header carries a quiet **"Sign in"**, and the footer a "Staff
  sign-in". Both go to the **app host's single sign-on**. The static site
  never handles credentials, sessions or tokens.
- Public elements offer **"Open in the app"** deep links: the replay opens the
  Site at the same day and time; a published chart opens it in Chart. A link
  lands on sign-in when needed, then on the target.
- Showing the links only to signed-in visitors is optional and cookie-less:
  a tiny same-origin probe on the app host that answers "signed in" or not,
  read by the page; the probe returns nothing else. Without it, the links are
  always shown, in a muted style.

### 17.15 One system, many sites: the public canvas (v10)

A program with several sites runs **one public-site system with a canvas per
site**.
- **A section library:** a hero showing the site's centre of gravity (the
  physical view of its main asset: a core, a loop schematic, a field map),
  key numbers, the delayed replay (§17.8), research, team, study here,
  partner or sponsor, intake forms, publications.
- **The canvas config lives in the site's repo:** the sections and their
  order, copy, images, brand tokens, and which catalog charts and datasets to
  publish. Config and content only; no code (the layering rule).
- **A site-scoped communications editor role** edits a site's canvas and
  submits it through the governed publish loop (§17.4, §17.7): the
  data-quality check and the delay apply unchanged. The role is granted per
  site through fine-grained authorization; an editor of one site cannot read
  or change another site's canvas. Copy and layout changes need the site's
  approver once; a new series needs a per-item approval.
- **Progressive capability:** a section is **omitted**, never shown as
  "coming soon", until the site has what it needs (published data for key
  numbers, a model series for the replay). The editor shows unavailable
  sections greyed with the reason.
- **A network page** presents all the sites as one program, each linking to
  its own site, with the configurable credit (§17.10) kept subtle.
- **Hosting:** one build, per-site paths or subdomains on the same public
  edge.

**Reuse map, updated:** the hero's physical view is the Site › System physical
view of each site's centre of gravity; the canvas editor reuses the
publication approval and "What's public" (§17.4); the role is a gate role
with a site scope; every other element is as in §17.13.

### 17.16 Open questions

1. **Triage home.** Log › Intake, or a dedicated inbox?
2. **Hosting.** A hosting provider per product, or one edge for every site?
3. **Approver.** Who may sign a publication approval by default: the site
   manager only, or a communications role too?
4. **Delay.** Is 24 hours the right default for the standing approval, and
   may a site shorten it for selected series?
5. **Public questions.** Off, FAQ-only, or a constrained model (§17.11)?
6. **The network page.** Owned by the program (which repo holds its config), and who approves it?

## 18) v11: devices — console, tablet, phone

§14.10 said nothing here blocks a phone layout. A product's control room needs more
than a layout: a fixed station, a handheld carried on a walkdown or used as a kiosk,
and a phone. This section is the device model every product inherits; a product adds
who may do what on each device.

### 18.1 Device classes

| Class | Role | Input | Identity | Limits |
|---|---|---|---|---|
| **Console** (a fixed station enrolled to a room) | the operator's working surface for the live process | keyboard (A · H · ? on the signing card), pointer, voice through the assistant | a site sign-in for the shift; a fresh sign-in or an enrolled voice for co-signs, relief and seals | presence is the station's enrolment; an unattended console cannot seal |
| **Tablet** (personal or kiosk mode) | walkdowns (one reading per screen, a large numeric keypad), kiosks for shared acts such as custody, field and lab work | touch, keypad, voice | personal: site sign-in plus a device lock; kiosk: a badge claims it for one person, a PIN or enrolled voice is the fresh factor, signs out after each signed act | acts tied to a room are signed only with that room's rotating code; outside the room the tablet reads but does not sign; it shows its mode on every screen |
| **Phone** | supervisors and managers away from the console; requesters following their own work | touch, voice where enrolled | a site sign-in in the app | never satisfies presence; acknowledges by default; signs only what the product allows by device, and only with an enrolled voice where the product's site setting permits |

Wall displays (§16.4) are not devices in this model: they are kiosk sessions with no
personal data and no input.

### 18.2 Responsive rules for the whole shell

| Shell part | Desk and console (> 1180 px) | Tablet (901–1180 px landscape) | Phone and portrait tablet (≤ 900 px) |
|---|---|---|---|
| Rail | the rail | a narrower rail | a bottom tab bar of the role's first four items plus More; a product's primary workflow item is never under More for the roles that use it |
| Right panel | the right panel | the right panel | a bottom sheet over the canvas |
| Attention bar | the bar | the bar | a single top badge |
| Tables (the DataTable) | rows | rows, low-priority columns dropped | card lists, each field labelled |
| Chart slots | a grid | a grid | stacked, each keeping its legend and lineage link |
| Side columns beside a list | beside | below | below |
| Wall displays | excluded | excluded | excluded |

### 18.3 What each persona can do on each device

| Persona | Console | Tablet | Phone | Desk |
|---|---|---|---|---|
| Operator | every act on the live process | walkdown readings in the room (room code); read only outside | read the run and their queue; acknowledge | — |
| Supervisor | co-sign, approve, seal | co-sign in the room | queue, co-sign only per §18.1, the escalation case | approvals |
| Site manager | — | — | escalation cases (Call · Hold · Ask), shift summary | reviews, evidence |
| Requester (experimenter, researcher) | — | kiosk custody, lab work | the status of their own request; acknowledge readiness | the product's workflow item, Chart, Library |
| Safety officer | — | surveys, releases | holds and dues (acknowledge) | approvals |
| Auditor or regulator | — | — | — | read-only review |

### 18.4 Offline and degraded, per device

The honest states (no heartbeat, node unreachable, waiting to send, feed stale,
unlogged operation, integrity break) are the same on every device. A tablet and a
phone keep drafts locally and show them as **pending, never as signed**; on reconnect
each is read back again and records both the time it happened and the time it was
recorded. A console without its node falls back to the product's printed backup form,
re-entered afterwards with original times. A kiosk that cannot reach the node never
shows "signed"; the releaser keeps custody until a signature lands.

### 18.5 Identity per device

Site sign-in on every device; a rotating room code to prove presence for room-bound
acts on a tablet; voice enrolment checked on the site node, which also counts as a
fresh sign-in; a fresh sign-in (or the enrolled voice) for seals and co-signs. The
device class is declared at enrolment and bound into every signature.

### 18.6 Open questions

1. **Phone signatures.** May a supervisor co-sign from a phone with an enrolled voice,
   or does a phone only ever acknowledge?
2. **Theme by device.** Is the dark console theme a device-class default, so that the
   shell's global default stays light?

## 19) v12: cohesion rules

A cohesion audit across every Site view design (v1–v12, the public site) produced
these rules. A product applies them; a product's frozen designs win inside their own
areas, and any conflict is raised to the owner rather than resolved silently.

### 19.1 One object, one drawer, one home

Selecting an object anywhere (the canvas, a list, a log entry, a chart mark, chat, a
search result, a phone) opens **its drawer**: the right panel, or a bottom sheet on
phones, with the same sections wherever it was selected: verdict, key facts,
disclosures, actions, linked chips, and the footer (provenance, *Discuss →*). "Open"
goes to the object's **one home page**. Acts (a form, or a signature that needs more
than one answer) are pages with a trail. A thing with a lifecycle has one home per
phase only when the phase changes who works on it (for example, an open run in
the live mode and a closed run in Log), and the rule is written down.

Chat renders object references as chips (the same chip as everywhere), and a chart
mark is a chip that opens its source.

### 19.2 Revelation: four depths

**Glyph → one-line summary on hover or focus → the drawer on click → the page on
Open.** A drawer opens with only the disclosure that matches the object's state. The
hover card is one shape everywhere: value · unit · source · as-of · quality.

**Never hidden at any depth:** a missed required obligation, the honest states, the
read-back before a signature, a hard-stop constraint, a hold, a refused publication.

### 19.3 One vocabulary

- **Status:** ✓ (good, signed, verified, agrees) · ? (waiting, unproven, degraded) ·
  ✕ (missed, failed, refused) · — (absent, not compared, stale), with ⏳ where a person
  must act. Always a glyph and a word; never colour alone.
- **The queue:** "Needs you" is the attention bar's queue, with sections To sign,
  Co-sign, Held drafts, Due. A product may show it beside its work, filtered.
- **The signing card:** one card; its verb names the act (Sign, Sign receipt, Sign
  approval, Certify, Co-sign, Seal); keys A · H · ? on every card.
- **Records:** an *entry* is a signed line in a logbook; a *record* is the object or
  package of record; an *event* is an append-only system fact that may draft an entry.
- **Trail:** item › sub-item › object, then "· lens"; the separator is "›".
- **Formats:** in the app, MM-DD HH:MM (seconds where they matter); in records and
  print, YYYY-MM-DD; units in display form (°C), stored symbols internal (`degC`).
- **Personas:** the generic archetypes in Axiom's docs; a product shows its role-pack
  names.

### 19.3.1 The word "sign" (owner decision, 2026-11-12)

The owner, 2026-11-12: "we're overusing the term sign all over the place in design. Signing is
really important in approving a log, a formal log that we want to sign. That's really the only
place where you would tell the user that they're signing something, or if they're signing a
document. I don't mind if there is a signature that's implied by virtue of the fact that they said
apply, or send, or whatever the action was. But we're overusing the term in the UI. It's overly
formal and awkward and only applies in one specific scenario, maybe one or two."

**The rule (design contract DC-21).** The interface says "sign" only when a person signs a formal
log entry (including co-signing one) or signs a document. Everywhere else the control and the copy
use the act's own verb (Approve, Reject, Create, Apply, Send, Save, Confirm, Pause, Retire), and
the signature is implicit: the act is still attributed and recorded exactly as today, and that is
said one step away in plain words ("recorded as approved by you, 14:15"), never as a ceremony.
"Sign in" is a different word and is untouched. Nothing changes in what the platform records or
signs underneath.

**The card (DC-10, reworded).** There is still one card. It is the **confirm card**: it shows
exactly what will be recorded, never folded, with Hold and Ask beside its primary and the keys
A · H · ?. Its title and primary are the act's own words. It appears for an act that cannot be
undone or that grants standing authority; in a create flow the Check step is that card; an act
that is safe to undo happens at once. This amends the "signing card" line of 19.3, whose list of
verbs becomes:

| Before | Now | Status |
|---|---|---|
| Sign, Co-sign (a log entry or a document) | Sign, Co-sign | Decided: these keep the word |
| Sign approval | Approve | Decided |
| Sign and create, Sign and turn on, Sign and retire | Create, Turn on, Retire | Decided |
| Sign receipt | Confirm receipt | Recommended; for the owner. If the receipt is a line in a formal log, "Sign" may stay there |
| Certify | Certify | Recommended, not decided: a formal act of its own, and not the word "sign" |
| Seal (close out a period of a log) | Close out, with "sign" kept in its copy | Recommended, not decided: it is a log act and may keep a formal word |

The queue's sections "To sign" and "Co-sign" (19.3) list log entries and keep their names. The
places that still use the word otherwise are listed in the audit in appkit [`docs/design/proposed-2026-11-13/people-agents-places/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-13/people-agents-places) (`SIGN-WORDING-AUDIT.md`).

### 19.3.2 Icons come from the icon set (owner direction, 2026-11-12)

The owner, 2026-11-12: "every time you do a design, you end up with a different set of glyphs.
This is going to lead to design inconsistency if we're not careful. If we find better glyphs,
let's replace the one there so everybody's using the same correct new glyph."

**The rule (design contract DC-22).** The one icon source is appkit's icon set. Every page, every
design mock page and a product's design take each icon from it; nothing draws an icon by hand or
carries a copy of a path. A design that needs a new or better icon changes the set first, in the
same change. Design mock pages read a file generated from the set, and a test fails when one
draws its own. The Library icon was replaced this way on 2026-11-12 (books on a shelf).

### 19.4 Colour

One token system for every theme (spec addendum v12). Themes are chosen per surface:
the app light (person's choice of dark), a dark **console theme as a device default**
for fixed control-room stations and walls, the public site, print, and a full-screen
alarm fill. **Brand tokens set the accent only, never a status token.** The accent is
never a status. A measured channel and its simulated counterpart share a series hue;
the simulated one is dashed and labelled. Held data is drawn neutral ("—"); publishing
it is the failure (✕). ISA-101 in live views and walls: normal is muted, colour marks
the abnormal. Status colours meet WCAG 4.5:1 on their panel and stay distinguishable
under deuteranopia and protanopia (worst pairwise ΔE ≥ 15 in CIELAB among ok, warn
and bad), and the glyph and word carry the meaning regardless.

### 19.5 One component for each repeated job

| Job | Canonical component | Placements |
|---|---|---|
| work waiting on a person | the attention bar's queue | the bar, beside a product's work (filtered), a phone's first screen |
| a missed required obligation | one case object | a full-screen alarm on the station that owes it, a wall's attention slot, notifications |
| a signature | the signing card | every book, every device that may sign |
| an audit | the Audit workspace in Log | ConCert queue, reviews, reconstruction, evidence, findings |
| a list of records | the DataTable | Log views, run tables, queues (card lists on phones) |
| a spatial view of the site | one map component | declared (System), live (the live mode), history (Log) |

### 19.6 Open questions (v12)

1. **Colour-blind "ok".** Green fails the ΔE target in light themes for some palettes;
   a blue "ok" passes easily but breaks convention. Which does a product adopt?
2. **Person-chosen theme on a station.** May a person override a station's console
   theme?

### 19.7 Where the app's data comes from (corrected 2026-10-30)

**Domain data reaches people from gold, read directly or published into the app's own
database; silver never serves. The app database also keeps the application's own state
(settings, accounts, drafts, layouts).**

The web app has its own relational (OLTP) application database. Gold publishes into it
what the pages need fast, so for domain data it acts almost like a cache or projection
of gold. It also holds application state that is not in gold: accounts and profiles,
settings, preferences, drafts, layouts, pins and chart slots, notifications. Gold is
the domain data, the business functioning of the system, and the data that must scale.
Silver serves nobody, directly or through the app database. A record a person writes lives
with its writer and reaches gold one way; a page reads its own tables and reads gold only
read-only through the gold door with an "as of" (ADR-241). This replaces the earlier
wording "every screen reads gold", which implied the app always reads gold directly.
Contracts: spec-site-view, addendum "the app database and gold"; MVP-11.

## 20) v13: people and agents

A twin of a site includes its staff. Until v13 the app had no place for people: no
directory, no profile and no account page, and agents lived apart from the people they
work beside. The owner asked for all the operators and all the actors in one place,
with agents near people, and for each person to see their own account and profile.

### 20.1 One actor concept

An **actor** is anything that can act or sign at a site: a **person**, an **agent** or
a **node** (a service). Every actor is a principal addressed `@name:context`. An
organization is a scope that groups actors, not an actor.

| Aspect | Person | Agent | Node |
|---|---|---|---|
| Identity | sign-in through the site's realm; key bound to the person | the agent's roster declaration; acts under its own handle | the node's key and certificate |
| Roles and permissions | roles (RBAC) and attributes (ABAC: site scope, clearance, qualifications) in one decision function | namespaces it owns; fails closed outside them; never delegates onward | service scopes |
| Qualifications | attributes with an expiry; checked at signing and snapshotted into the record | none; an agent never signs a record | certificate validity |
| Duty | on shift, at a station, on call: derived from shift and watch records, presence and the roster | running, paused or stopped; last report | healthy or degraded |
| Delegation | owns agents and is accountable for what they do | acts for a person within a scope; the person stays accountable | — |
| Devices and enrolments | personal devices, enrolled stations, voice enrolment, room-code presence | — | the devices it serves |
| Trail | signed acts, log entries, approvals | actions, proposals, approvals asked and given | anchors, deploys |

**No second store.** An actor's facts stay where they already live (the gate's
accounts and keys, the directory's groups and attributes, the agent roster, attest's
devices, intervals and signed records, the audit chain, the reachability profile).
The directory of actors is a read across them. Every agent names an accountable
person; one without is a finding.

### 20.2 Placement

Three options were judged against the minimal rail and "two clicks to anything":

| Option | For | Against |
|---|---|---|
| (a) a rail item only | one place for people and agents | who is on duty now is site state, and belongs in Site too |
| (b) a Site system only, agents in Automate | no new rail item | splits people from agents; a person is three clicks deep |
| **(c) both (recommended)** | a rail item holds the directory; Site › System holds "on duty now"; the live mode keeps its own "at the station" block | one more rail item |

The recommended rail item, **Team**, sits after Log (Log records acts; Team records
actors). Every actor has one drawer, opened wherever a name appears (a log entry, the
signing card, a custody record, chat), and one profile, two clicks from anywhere.
Personas may hide Team; its label is an owner decision.

### 20.3 Profiles and privacy

- **A person:** roles, qualifications and their currency, current duty, recent signed
  acts, the work items they are on, the agents they own, contact (verified channels
  only, shown as buttons; the address stays hidden).
- **An agent:** what it does, its accountable person, scope and what it may not do,
  skills and tools, recent actions and approvals, health, a pause switch (stops new
  work at once; resuming needs the accountable person), and its ConCert status.
- **A node:** what it serves, health, certificate validity, what it anchors.

| Viewer | Sees of a person |
|---|---|
| anyone signed in at the site | name, roles, on duty or not, contact buttons |
| supervisors, site manager, admins | + qualifications and currency, shifts, signed acts |
| an auditor (time-boxed grant) | + the qualification record behind each signature, read only |
| the public site | nothing unless the person opts in: name, role and photo, through a publication approval; never duty status |

### 20.4 My account, and Settings

**Account** (avatar menu) is personal: what expires soon first, then profile, sign-in
and sessions, keys (self-minted; rescoped in place, same token), devices and voice
enrolment, notifications, preferences (theme, landing, rail), my agents and
delegations, my qualifications, my data (export) and my change log. **Settings** is the
site's configuration for admins: flags, data and retention, health, the full change
log. Admin work on people (invitations, roles, the roster, offboarding) lives in Team.
A change of role, scope or upgrade never forces a person to sign in again.

### 20.5 Admin

- **Invite:** the existing invitation flow; intake requests become invitations.
- **Roles and qualifications:** assigned with evidence; every change in the change log.
- **The roster:** who may hold each station on each shift, checked against
  qualifications when assigned, when published, and again at the moment of relief,
  because a qualification can lapse in between. The relief check runs before the
  signing card; a failing check replaces Sign with the reason and the way out. The
  roster is a plan; the log records who actually held the station.
- **Offboarding:** one checklist: end sessions and keys at once, remove from future
  roster slots, transfer the agents they own to a new accountable person, keep every
  record they signed unchanged.

### 20.6 Ties

| Area | Tie |
|---|---|
| the signing card | shows the signer's identity and the qualifications checked; snapshots them into the record |
| Log | shift and watch changes are entries; duty status is read from them |
| ConCert | "qualifications are current" and "agents act within scope" are certifiable controls |
| the product's workflow item | the groups of people on a work item appear on their profiles |
| Needs you | expiring qualifications, keys and grants (watched by the expiry steward agent) |
| the public site | an opt-in, role-based team section |
| Model | who owns each pipeline stage |
| partner sites | actors are site-scoped; the scope selector filters Team |

### 20.7 Phasing

- **P1 (MVP):** Account; Settings for admins; Team with people, agents and nodes,
  filters by kind and role, the actor drawer; invitations and roles (MVP-08, MVP-14,
  MVP-23).
- **P2:** the public team section (opt-in).
- **P3:** signed acts on profiles.
- **P4:** duty status, the People and shifts system, qualifications with expiry, the
  roster with the qualification check, full agent profiles (with the agent spine).
- **P7:** qualification currency as a ConCert control; auditor views.

### 20.8 Open questions (v13)

1. The rail label: Team, People or Crew; shown by default for every persona?
2. A full profile for every colleague, or only the drawer?
3. Who sees qualifications: supervisors and admins only, or everyone?
4. Does a lapsed qualification block a roster assignment, or only warn until relief?
5. Do nodes belong in Team, or only in Site › System?

## 21) As-built anchors: Library, Charts, Chat (corrected 2026-10-30)

**Search entry (2026-10-30, owner's veto open).** Search is not a rail item. A clearly
visible magnifier button sits beside the wordmark, and ⌘K works anywhere; both open one
search modal. This is the as-built SoilMetrix pattern (a Search button beside the logo
opens `SearchModal`), ported to appkit as `SearchPalette`. The modal is the as-built
typeahead, grouped by name and in contents, across Library, Chart, Log, Team and chat
history. On a phone the magnifier sits in the top bar. Every rail listing earlier in this
PRD that starts with "Search ·" is read without that item.

**Expand and displays.** Anything worth watching can fill a screen, with one way in and
one way out. The ⛶ sits top right of a chart, table, tile, group, section or page; Esc
or ✕ in the same corner leaves. It generalises the as-built ⛶ on chart cards, chat
charts' Expand and the Charts page's wall-display mode into one appkit behaviour,
`Expandable` / `DisplayMode`. Display mode, kiosk display URLs (a read-only, site-scoped,
revocable "display" actor) and auto-cycle ship with walls in P4.


*Rule: a design starts from the as-built screen, adds only within it, and marks
each addition NEW. Where an earlier section of this PRD reads otherwise, this
section governs.*

### 21.1 Library

The Library is the as-built Dropbox-model browser (ported from an earlier
product, appkit #79 and #81). The rail name is always **Library**; "Book" as a
rail item is retired.

- **Views:** Home (Frequently used, New, Starred), Recent, Starred, Shared with
  me, As uploaded, All files, Deleted files.
- **The list** is the core DataTable, with a selection bar; copy and move.
- **Version history** on every file; restoring a version makes a new version.
- **Search** by name and by contents; the typeahead groups results by name and
  by contents.
- **The classification tree** (the Data Book, renamed by a product's book
  scheme) appears as folders inside the Library.

Additions, all inside the browser:
- **NEW: generated record packages** are a read-only generated folder (for
  example, one per sample), not a separate viewer.
- **NEW: scans of paper forms** keep their original times (when the paper was
  written), not the upload time.
- **NEW: results and publications** are files in a folder. There is no
  separate publications page.

There is no separate Library or Book screen and no record-package viewer.

### 21.2 Charts

Chart is the as-built appkit Charts page: groups of graphs (the group picker
and Groups menu), the window selector ("Day · latest"), **+ Add graph**,
**Share link**, the settings gear, **Layout**, wall-display mode, and the
per-graph **Configure** panel (site, feeds, channels), with **Discuss** and
palettes. Graphs are ChartSpec 2.0 kinds: line, flame, 3D scene, Trame,
VERAView, gold, and twins. Charts read gold.

Additions layered on it:
- **NEW: Pin to…** places a reference to a chart (not a copy) in a chart slot
  in another view or on a wall (§16.3).
- **NEW: wall displays** extend the existing wall-display mode (§16.4).
- **NEW: chat-suggested chart types:** chat offers two or three valid chart
  types inline for the data and the question, switchable; **Save** adds the
  chart to the Charts catalog.

### 21.3 Chat

Chat follows the as-built chat of an earlier product (the familiar assistant
pattern). On landing, one input is front and centre with action cards beneath
it (in appkit this is behind a flag; turning it on is the change). Once a
conversation starts, the input moves to the bottom and the conversation
scrolls up. Answers carry inline charts, tables and citations, with type chips,
**Save** and **Expand**.

**Exactly one chat input per screen.** Any other "Ask" box on a page routes to
that one chat: **Discuss** and *Discuss →* open the chat slide-out with the
selection attached. A page never hosts a second chat. Search is separate: the
rail's Search and its typeahead.

### 21.3.1 Chat and agents (approved 2026-11-12)

**Status: approved by the owner, 2026-11-12**, with the screens and the prototype in appkit
[`docs/design/proposed-2026-11-12/chat-and-agents/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-12/chat-and-agents). Direct links: [README.md](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/chat-and-agents/README.md), [gallery.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/chat-and-agents/gallery.html), [prototype.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/chat-and-agents/prototype.html). GitHub shows the HTML as source; clone the repository and open the file to view it.
Nothing is built. It amends 21.3: the landing and the one chat input stand; what a conversation is
changes. The parts at the end ("One Timeline", "The Chat list", "Marks", "Who is in a
conversation") were decided the same day, when the owner reviewed the prototype.

**The principle, in the owner's words.** "A session is an implementation detail and does not
appear in the UI; a conversation with an agent is one continuous timeline; topics are the person's
own way to separate it." On screen the words are "conversation" and "topic"; "session" stays in
code and documents.

**Agents and conversations.**

- Every conversation is with a named agent. The assistant is the first named agent, the one that
  routes to the others, and it keeps the product's name.
- A person has one continuous conversation with each agent, the assistant included. It scrolls
  back as far as it goes and is divided only by day lines. The landing is unchanged and is the
  assistant's front door.
- An agent is one object with four views: Chat, Activity (the Timeline filtered to the agent; see "One Timeline"), Files, and
  Tune (its profile, its dials, and what it remembers about the person). The agent drawer is the
  same agent opened from a row, with "Talk to it".
- An agent is listed under Chat in the rail only after the person talks to it directly, or pins
  it. When the conversation view ships, History leaves the rail.
- A product can turn all of this off with one feature, `chat_agents`.

**Topics.** Typing `#name` in a message makes or reuses a topic and puts the message in it; `#`
opens a picker of the person's topics with "New topic" last, the same component as the `@`
picker. A topic covers that message and the answers to it; the input keeps it as a visible chip
until the person removes it or types another. A message has one topic at most and can be added to,
moved or taken out afterwards. Topics belong to the person and may hold messages with several
agents; nobody else sees them. Renaming onto an existing name merges. An agent may suggest a topic
once and never applies one. In a comment or a signed entry `#` is plain text. A per-person,
remembered switch shows chats by topic: the person's topics under each agent in the rail, and a
topic that spans agents under Chat. In that view, messages with no topic are reached only through
the whole conversation. Topics have nothing to do with sessions.

**Getting back to a thing.** Scroll, and go to a day; find in this conversation; search across
agents, landing on the message; the person's topics; and the Timeline's Mine filter. There is no
list of conversations.

**Files.** The files that are part of a person's conversation with an agent, given or made, in a
conversation or a scheduled run. It is a view of Library items, never a second store. A row is a
name, where it came from in words, and when; a file opens in the one drawer. Label, retention and
access are Library's; an item the viewer may not read is neither listed nor counted. A topic is a
filter. Retiring an agent leaves its files in Library and its Files view readable (Axiom #1898).

**What an agent remembers.** Memory is what the system took away from what was said, done, signed
and kept, to use later; it is per person, and an agent's memory is a filter over a person's
fragments. In Tune, "What it remembers about you and your work" lists the person's own items, each
with where it came from, Correct and Forget. An answer that used memory says so in one quiet line.
Deleting part of a conversation says what was remembered from it and offers to forget it. Today a
turn is stored twice (the chat store and an episodic fragment), a fragment's source is a session
and not a message, a recall is not stored with the answer, the journal's `memory` lane is fed by
nothing, and Correct is not built (Axiom #1904).

**Calling another agent with `@`.** The exchange lives where it was typed, the other agent is
named wherever it speaks, and each side keeps one record.

- *A person asks agent B inside agent A's conversation.* The message and B's answer stay there,
  B's answer attributed to B. B receives only that message and its attached files; when a file is
  included, the page says so before sending. The next message goes to A; A may read B's answer.
  B's Activity gains a row that links back. A mention does not list B under Chat; "Continue with
  B" opens B's conversation with the exchange attached.
- *Agent A asks agent B itself.* One step row in A's conversation, which opens to what was asked
  and answered. B never does more for A than the person behind A's request may ask of B; B's own
  ceiling and asks hold; an ask from B goes to that person, once, in Needs you. A person asks A, A
  may ask B, and B may not pass it on; a refused hand-off is said in plain words with one action.
- *B is at another site* (`@name:context`, ADR-020). It is marked "at <site>" wherever its name
  appears. Only the message text leaves, unless the person attaches a file that may leave; one
  that may not is held before sending, with the reason. A named person decides which sites may be
  asked; a site that may not be asked is not offered, and typing it starts the no-access flow. No
  answer within the deadline says so, and says delivery is not known. An agent does not ask across
  sites by itself at first. No protocol is chosen here (Axiom #1897).

| | A person asks B in A's conversation | A asks B itself | B is at another site |
|---|---|---|---|
| Conversation | A's: the message, and B's answer | A's: one step row | A's, marked with the site |
| Activity | B: "answered <person>, in A's conversation" | A: "asked B"; B: "answered A, for <person>" | Here, A's row if A asked; there, the asked agent's |
| Files | A's and B's; one object | B's; A's if A's reply uses it | A's, if a file arrives |
| Library | The file's home | The file's home | Its home here; the original stays there |
| Journal | One call record, with the chain | One call record, with the chain and its depth | Here, what left and who allowed it; there, what arrived |

**Records, and one index.** What was said is a conversation, seen by the people in it. What was
done is a run. What was signed is a logbook entry. What was kept is a file. What was remembered
is a memory fragment. The journal (ADR-163) indexes each by reference and holds none of their
content. Conversations and memory are not on it yet (Axiom #1903, #1904).

**What the harness must do so a session never shows** (Axiom #1908). A session is a stored unit
with an id, messages, a name, usage and an owner; it keys the stored messages, the model's context
for a turn, checkpoints and usage, and does not record its agent (Axiom #1895). A call to another
agent runs in a fresh one-turn session of the asked agent that is never stored, which stays.
Missing: a conversation above the session, ordering a person's sessions with one agent and giving
every message one position; a rule for rolling to a new session (none exists; compaction is a
stub) that carries a summary, the last turns and what memory serves, and never rolls inside a
turn; retrieval over the conversation's own stored messages under the same ownership check;
search hits at message level; one store for the command line and the web. The agent holds the
recent part of a conversation, remembers what was taken from the rest, and looks back when asked.

**Skills.** Built: `agents.address`, `agents.ask`, `agents.chains`, `agents.chain_stop`,
`chat.history`, `library.list`, `library.show`, `library.upload`, `memory.forget`, `features`.
Proposed: the agent on `chat.history` rows; the conversation and the requester on
`agents.address` and `agents.ask`; `chat.topics`, `chat.topic_show`, `chat.topic_tag`,
`chat.topic_untag`, `chat.topic_rename`, `chat.topic_archive`; `library.list` by agent,
conversation or topic; memory listed by agent and `memory.correct`; a per-site permission to ask,
with `gate.request`.

**Acceptance.**

| Row | Requirement |
|---|---|
| CAG-A1 | A newcomer's first screen is today's landing: one input, action cards, no agent list |
| CAG-A2 | An agent is listed under Chat only after the person talked to it directly, or pinned it; a mention does not list it |
| CAG-A4 | There is exactly one chat input on every screen (DC-06) |
| CAG-A5 | Files lists only Library items linked to the conversation; an item the viewer may not read is neither listed nor counted |
| CAG-A8 | A person's `@` message and the asked agent's answer are stored in the conversation where it was typed, and the answer names its agent |
| CAG-A9 | The asked agent receives only that message and its attached files |
| CAG-A11 | A call between agents is one row in the calling conversation and one in the asked agent's Activity, with the same chain |
| CAG-A12 | An agent asked on a person's behalf is refused anything that person could not ask it directly; its ask goes to that person, once |
| CAG-A13 | A hand-off past the limit is refused before anything runs |
| CAG-A15 | Nothing but the typed message leaves the site unless the person attached a file that may leave |
| CAG-A19 | With `chat_agents` off, the rail, Chat, History and the drawer are exactly as today |
| CAG-A21 | A conversation with an agent is one list back to its first message, divided only by day lines; nothing marks a session |
| CAG-A22 | The word "session" is on no screen |
| CAG-A23 | Typing `#` opens the topic picker; sending puts the message in the topic |
| CAG-A25 | A topic belongs to the person and may span agents; `#` in a comment or a signed entry creates nothing |
| CAG-A27 | Find and search open a conversation at the message found |
| CAG-A28 | "What it remembers" lists only the viewer's own items, each with its source, Correct and Forget |
| CAG-A30 | After the conversation view ships the rail has no History item |

The full list (CAG-A1 to A32) is in the appkit folder's README.

**Design contract: proposed edits, for a follow-up change to `docs/design/README.md`.**

- DC-03. Today: "Chat · Site · (the product's own items) · [Model] · [Chart] · Log · Library ·
  Team · History at the bottom." Proposed: "Chat · Site · (the product's own items) · [Model] ·
  [Chart] · Log · Library · Team. Agents a person talks to are listed under Chat; they are not
  rail items, and the list is empty until the person talks to one." History is removed when the
  conversation view ships, not before, so the edit lands with that change.
- DC-06. Unchanged in force. Proposed addition: "A conversation with an agent is one continuous
  timeline, and the input belongs to the agent whose conversation is open. The foot of a
  conversation is the input's; the Timeline panel is not shown there."
- DC-09 gains: "✦ opens the assistant with the selection; from an agent's own row it opens that
  agent." DC-15 gains: "An agent's home is its conversation. A file's home is Library; an agent's
  Files is a view of it."

They are not applied in this change: the DC-03 wording depends on when History leaves, and the
DC-06 sentence about the Timeline depends on "One Timeline" below.

**Order.** (1) "Talk to it" in the drawer; History grouped by agent only as an interim. (2) The
Timeline's Mine filter and the panel beyond Log. (3) The conversation view and History leaving the
rail: after the logbook units on chat and Control, and after Axiom #1908. (4) Topics, the by-topic
view, Find and search. (5) Files; what it remembers. (6) `@` in a conversation and the step row:
after Axiom #1896. (7) Another site: after Axiom #1897.

**One Timeline (written after the approval; for the owner's review).** The owner saw a timeline in
several places and asked for one coherent thing.

| Where | What it shows | State |
|---|---|---|
| Log, the first tab of its switcher | Every journal event the viewer may see, by time; lane and level; Tail | Built (unit J2); approved 2026-11-11 |
| The foot of a log's page | The same events, scoped to the open entry and its links, or the whole site; a chevron; "Open in Log" | Built, on the Log page only |
| The foot of any other page | The same panel, scoped to what the page shows | Approved, not built (drawn under Control) |
| An object's drawer | That object's lanes, live (Tail) | Built (unit J3) |
| A chat message | A live block for a long step (Tail) | Approved; not built |
| Control; an agent's Activity; a step in a conversation | Runs, one row each | Built; approved |

The rule (approved 2026-11-12). There is one Timeline: the journal's events in time order, one
component in two sizes, and **it is never shown twice on one screen**. It is the body of the
screen when it is what the person came for, and a panel at the foot when they came for something
else and want to see what is happening around it.

| Screen | Its body | The Timeline there |
|---|---|---|
| Log, the Timeline tab | The Timeline, the whole site | Body; no panel |
| Log, a log's own tab | That log's entries | Panel, scoped to the log |
| An agent, Activity | The Timeline, filtered to the agent | Body; no separate list of runs, no panel |
| An agent, Files and Tune | The files; the profile, dials and memory | Panel, scoped to the agent |
| An agent, Chat; the landing | The conversation | None: the foot belongs to the one input (DC-06) |
| Control | The stream of runs | None while the body is the stream |

Its controls are the built ones (a lane, a level, Tail) plus one: Everyone or Mine. Mine is what
the viewer asked for and what was done or remembered for them, never a conversation's text; it is
a filter, not a place. A run is a row in the Timeline, not a second list: it keeps its state as a
glyph and a word, a plain title and the time, and opens in place to its log and to what it could
not establish; a step in a conversation is that run inline. An agent's Activity starts on the
lanes that show its work. The panel starts closed, as one line that says in words what it is
scoped to ("What is happening around this log"); "Open in Log" turns it into the body, carrying
the scope. The 11 November Tail design drew the panel under Control; that is superseded on this
point. An earlier sketch of Log as two tabs with four filter chips is withdrawn. A calendar's
"timeline mode" uses the same word for a different thing: "Timeline" is kept for the journal's
view.

**The Chat list (approved 2026-11-12).** Under Chat, people, agents and the person's topics are
one list. The rail shows at most five rows (pinned first, then most recent), then "More…". A row
is a mark, a name and, only when something waits for the person, one quiet count at the right
edge: a numeral, with no icon and no words, not red and not the warning hue, absent at zero, the
same on every kind of row. It is a part of the one "Needs you" total, never a second tally; the
words are one step away, as a tooltip and at the top of that conversation as the single ask with
its action. More opens the full list: search; All, Agents, People, Topics; keyset paging and
rendering only what is on screen. A row can be pinned, or removed from the list, which never
deletes the conversation. The `@` and `#` pickers are the same list with the same marks. Topics
are rows in this list, not nested under agents. Where a topic's `#` mark is beside its name the
name is plain; inline in a message and on a chip it keeps its `#`. A newcomer has no list.

*Views of the list (proposed 2026-11-12).* The default needs no choice. One "View" control, one
step away (the full list's header, and a small menu on Chat in the rail), offers four named
views, remembered per person and applied to the rail's short list and the full list: Recent (the
default), A to Z, By kind (Agents, People, Topics) and By time (Today, Yesterday, This week,
Earlier). Pinned is always on top. The rail stays at five rows in every view, with a group header
as a small plain label; in the full list a group folds, and a folded group shows the sum of what
waits in it as the same quiet numeral. The full list renders only what is on screen, pages by key
and keeps search at the top. "Chats by topic" stays the conversation-level switch. Not now: custom
sections, drag to reorder, folders, colours, per-row settings, saved filters, muting a row.

**Marks (approved 2026-11-12).** A person is always a circle with initials. An agent is never a
circle: it is one of a small family of plain polygons with two dot eyes, filled with one of the
agent tints. The assistant is the product's own mark in the default shape, untinted. A topic is a
bare `#`. Kind is told by circle or polygon with no colour needed; one agent is told from another
by its name first, then its shape, then its tint, so where no name is beside an agent's mark its
initials are. A shape or a tint never says status, role, rank or risk; another site is said in
words beside the name, and so is a state (paused, waiting, silent, failed): a mark is never
changed, greyed or recoloured to show one.

- *The family.* Seven plain polygons on the icon set's grid and stroke: hexagon (the default),
  diamond, pentagon, shield, trapezoid, trapezoid with a wide base, parallelogram. They are the
  ones honestly distinct at 20 px; the family is not padded with notches, antennae or cut corners.
  A circle, an octagon, a rounded or plain square, a triangle and a star are never agent shapes.
- *The tints.* Six tokens of their own (`--axk-agent-1` to `-6`), not the chart series tokens:
  none is the identity orange, the accent or a status hue, and each is checked for the eyes and
  outline in the light, dark and console themes.
- *Defaults.* Every agent the platform ships has a default polygon, tint and name. There are
  sixteen shipped types and seven shapes, so a shape repeats in a different tint: shape and tint
  together make a default unique (42 pairs), and types that often appear together differ in shape.
  An agent made from a type starts with its type's shape, a different tint and a corner dot. An
  agent described from nothing takes the next unused pair at the site, the hexagon first.
- *Name and look.* On the agent's Tune view the person who answers for the agent (or an
  administrator) may change its display name, its shape (the family only, never a circle) and its
  tint (the six only), and put them back to the default. The change applies to everyone at the
  site; there are no per-viewer skins. The handle (`@name:context`) never changes and stays the
  key for addressing, authorization, the journal and audit. A name may not equal another agent's
  name at the site nor a person's name in the directory. Each change is one journal row (audited,
  not signed). Past messages and Timeline rows show the name and look in force; a renamed agent
  shows its first name one step away, and search finds it by either. The platform's own agents may
  be renamed and reshaped at a site in the same way; the assistant keeps the product's name and
  mark (Axiom #1911; appkit #184).
- *Liveness (decided 2026-11-12; the rule is "be subtle").* An agent's mark may move only while
  the agent is working right now (a run of its is in progress). It never means recency or "needs
  you", and a paused, silent, failed or idle agent never moves. A person's circle has one cue
  only, the sparkle on its edge while the person is here now (proposed with people's activity,
  below).
  The motion is one small repertoire, each part at the smallest size that is seen when looking at
  the mark and not seen otherwise: a breath of the tint (a few percent of its opacity, one cycle
  in about 7 seconds); a blink of the two eyes (about 300 ms, every 10 to 16 seconds); a glance
  (the eyes shift together about 1 px to one side and return, about 700 ms, every 40 to 70
  seconds; the shape stays still); and a sparkle (a small bright glint, always lighter than the
  outline, that appears on the outline, travels a quarter of the way round in under a second,
  twinkles once and fades, every 50 to 80 seconds). The sparkle MUST be drawn on the mark's own
  outline and never light a pixel of the face, the tint or the eyes. Each gesture has its
  own timer, seeded from the agent's handle and the gesture's name, so no pattern repeats and two
  agents are never in step; a gesture that would start while another is in flight waits a moment.
  There is no gesture when a run finishes. The motion MUST use opacity, transform and dash offset
  only, with no colour flash and nothing that changes size or layout; MUST be driven by the
  debounced state and never by each event, starting after the state has held and letting a
  gesture in flight finish before the mark goes still; MUST appear only in the rail's short list
  and the open agent's header, never in a long list, a picker, a thread or the Timeline; MUST
  pause while the window is hidden; MUST be absent with reduced motion, where a small still cue
  says the same; and MUST never be the only signal: the state is in words one step away and in
  the accessible label. If the whole ever pulls the eye from across the page it is made rarer, not
  bigger.

- *People's tints, and activity (decided 2026-11-12).* A person's circle is filled with one of
  the same agent tints, given from the person's principal and changed only by that person. A
  mark's tint shows activity in four steps, the same for an agent and a person, computed from a
  coarse last-active time (to the hour) and changed without animation: active now (an agent has a
  run in progress; a person did something in the last few minutes): full tint, with the gestures
  above for an agent and the edge sparkle for a person; recent (within a day): full tint, still;
  idle (one to seven days): the tint half way to grey; dormant (over seven days): grey. Only the
  fill fades; the outline, the eyes or initials and the name keep their contrast. This is not a
  status: paused, waiting, silent and failed stay a glyph and a word beside the name, and the
  last-active time is one step away in words. A person's activity needs a server signal that does
  not exist yet (the last time they did something in the app at the site, to the hour, visible to
  people at the site who can already see them in the directory), and a person can turn their own
  activity display off (Axiom #1909). An agent's last-active time comes from the journal (Axiom
  #1911).

**Attention signalling (to be designed).** How the app signals that something needs a person is
to be designed once for the whole system (Axiom #1912). The owner's principles, 2026-11-12: a
count of things that need your attention is used deliberately and sparingly, in very few key
places; it probably is not red and may have no colour; the counts are one total and its parts,
the same on the web and the phone, and what is cleared in one place clears everywhere; it must
scale to hundreds, with bulk handling and handing off, possibly to agents, with limits and a
record; and strict rules say who may raise a count and for what (a problem is shown only to the
person who can act on it, with the action). Until then the Chat list's count above is the only
addition.

**Who is in a conversation (approved 2026-11-12 as the direction; not built).** A conversation
between two people is new: a stored conversation has one owner and no participants today, and no
PRD describes a direct message (Axiom #1909). The model, stated once: a conversation has an anchor
(an agent, a person, or a `#topic`, which is a view over the person's own tagged messages) and
participants (whoever has spoken or been addressed in it). `@` addresses an agent or a person from
inside any conversation; the exchange stays where it was typed; each message is attributed to its
author.

When an agent is asked inside a conversation with a person: its answer is seen by everyone in that
conversation, and the page says so; it acts with the asker's rights and never the other person's;
it receives only the message addressed to it and its attached files; an ask it raises goes to the
asker; a file it makes is opened only by people who may read it, and a participant who may not
read it sees that one is attached, with the way to ask, and nothing more; it remembers for the
asker only; journal rows are seen by the participants only and never carry text. A person is not
invited into an agent's conversation, for now: one exchange is shared with a person, which
continues the conversation with that person and carries a link. A topic is never shared by being a
topic, and two people using the same name have two unrelated topics. Conversations between people
are their own later unit. What groups of people look like (more than two people in one
conversation, its mark and its row) is an open question for later.

The privacy rules, as a person would say them: what I say to an agent is mine; in a conversation
with a person both of us see everything in it, including what an agent answers there; an agent I
ask does only what I may ask it; an agent sees the message I send it, not the rest; an agent
remembers things for me, not about the other person; a file is opened only by people who may read
it; my topics are mine.

| Row | Requirement |
|---|---|
| CAG-A31 | There is one Timeline component; no screen shows it twice; the panel starts closed and names its scope; an agent's Activity is the body size filtered to the agent |
| CAG-A33 | Kind is told by the mark's shape in every theme with no colour needed: a circle only for a person, a straight-edged shape with two dot eyes for an agent, a bare `#` for a topic; no mark is changed or recoloured to show status |
| CAG-A33a | Where a topic's `#` mark is beside its name, the name has no `#` |
| CAG-A33b | "Name and look" is offered only to the person who answers for the agent, offers only the shape family and the agent tints, refuses a name another agent or a person has, shows everywhere at the site, writes one journal row and never changes the handle |
| CAG-A34 | The rail lists at most five rows under Chat and one "More…"; a newcomer has none; a row shows at most one count, which is a part of the Needs you total |
| CAG-A35 | Removing a row never deletes a conversation |
| CAG-A37 | A message in a conversation between two people is readable by exactly those two |
| CAG-A38 | An agent asked in it acts with the asker's rights only, and its memory gains nothing about the other participant |
| CAG-A39 | A participant who may not read an attached file cannot learn its name or contents |
| CAG-A40 | A topic lists only the viewer's own tagged messages |

### 21.3.2 People and agents: one object, three lenses (approved 2026-11-12)

**Status: approved by the owner, 2026-11-12**, with the screens, the journeys and the prototype in
appkit [`docs/design/proposed-2026-11-13/people-agents-places/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-13/people-agents-places). Nothing is built; chat is on hold, and the first three stages need no conversation view.
It settles how people and agents appear under Chat, under Site › Automate (Control) and under
Team, and amends 21.3.1 and §20 where they said otherwise.

**The model.** Chat, Team and Control are three ways to arrive at the same two things: an actor (a
person or an agent) and its work. Each answers one question: Chat, who do I talk to; Team, who and
what is here, and who answers for it; Control, what are the agents doing, what did they do, and
what will they do next. An object opens the same way from every place (DC-19).

- **An agent's home is its own page**, with four views: Chat, Activity, Files and Tune. Chat is
  the first; where chat is off or not yet built the page opens on Activity. This replaces "its
  home is its conversation" in 21.3.1. The one drawer is the agent's preview: Activity and Tune by
  the same components, "Talk to it", "Open" and "More…". What an agent may do and who answers for
  it open Tune; there is no Profile tab, and no separate agent profile in Team (this amends §20.3,
  "an agent's profile").
- **An agent's Activity opens with "Coming up"**: its automations, above its agent runs.
- **The Check card in a conversation is the same flow as "+ New automation".** When a person asks
  an agent to do something regularly, the agent answers with the Check card the form uses (what
  runs, when in words, done by, who is told, what it asks first; Confirm, Try it once now,
  Change). Once confirmed, the automation's row stays in the conversation at that point and is the
  same row as under "Coming up" and under Control's Scheduled.
- **A name in an answer is a chip** that opens the one drawer.
- **A person mirrors an agent.** A person is one object, opened in the same drawer from Team, from
  a name on any row and from a row under Chat. "Message" is the drawer's one primary action; when
  conversations between people exist (#1909) a person's page has two views, Chat and Profile.
  Until then "Message" is the verified contact channel (§20.3).
- **Team is the home of "New agent"** ("+ Invite ▾ New agent"), and the flow ends on the new
  agent. For an administrator Team's agent rows also say what each acts alone on. "My agents" in
  Account (§20.4) is Team filtered to "I answer for". The offboarding checklist (§20.5) lists the
  automations a person set up beside the agents they answer for.

**Acceptance** is PAO-A1 to PAO-A10 in the design's README. **Open:** what groups of people look
like; attention signalling (#1912); whether Control later folds into a view of the Timeline (this
model does not prevent it).

### 21.4 Retired names

- **Steer** is retired. The behaviour lives at Site › Automate; **Control** is
  its route (`#/control`) and its ⌘K name (D27). Never write Steer as a current name.
- **Book** is retired as a rail name; it is **Library**.

## 22) v14: calendar and schedules

Things happen at a point in time all through the app: a request is booked on a
station, a shift ends, a calibration falls due, a credential expires, a nightly job
runs, a publication goes out. Each feature already records its own times; there is no
way to see them on one schedule. The owner asked for a visible way to show schedules
without building a calendaring product: the traditional day, week and month views,
Google Calendar's behaviour where it applies, a timeline (Gantt) where resources and
spans matter, and views embedded where they are useful, with a link to the full page.
A program tracker with its own schedule is explicitly **not** part of this.

**The rule.** The calendar is a **view over items that other features own**. It reads;
it never stores, creates or edits an item. Creating, moving and cancelling stay in the
owning feature, behind its own permissions and approvals, and every item links back
to it.

### 22.1 Built from what exists

| Part | Starts from | New or lifted |
|---|---|---|
| Week and day time grid | a product's as-built prototype week calendar (Outlook-style overlap columns, all-day row, workday and weekend shading, the now line, Prev / This week / Next) | **lifted**; the product's own calendar tab renders through it, so the two cannot drift |
| Navigation semantics | appkit `WindowBar` (the source does the window arithmetic; the bar reports nudges) and `DayPicker` | reused |
| Time labelling | appkit `figure/clock` ("said, always") | reused rule; the calendar says **site time** where a figure says UTC |
| Status glyphs and colours | the v12 status palette and glyph set | reused |
| Embedding | the chart slot (PRD §16): a reference placed in a view, per-persona visibility, off takes no space | pattern reused for the **schedule slot** |
| Event carriers | the scheduler's calendar protocol (`EventSpec`: summary, start, end, rrule, timezone, status line, links, metadata) and its time slots (`planned_*`, `actual_*`, `resource_key`) | reused field names |
| Month, agenda, timeline mode, mini month, the schedule slot wrapper, the `ScheduledItem` read contract | none in appkit, the product apps or the reference partner app | **new** |

### 22.2 The minimal function

**Views.** Day, week, month, agenda (list) and **timeline**: resources as rows, time
across, for anything where the question is "what occupies this resource when"
(stations, positions, instruments, executors, people on shift). A product may give a
timeline a frozen first layout (its own week-by-resource table) and the timeline mode
renders it.

**Navigation.** Today, previous and next, a date picker (mini month), a view switch,
and the Google Calendar keys: `t` today; `d` `w` `m` `a` day, week, month, agenda; `l`
timeline (new); `j`/`n` next, `k`/`p` previous; `g` go to a date; `/` search; `Enter`
open the item in its owner; `Esc` close; `?` all shortcuts.

**Filters.** By source (each contributing feature), by scope (the scope selector), by
resource, by status, and "mine". A filter never hides an item's existence from a
count; it only stops drawing it.

**Drag.** Only where the source declares a move verb, and the drag calls that verb (for
example "propose another time", which an approver then confirms). The calendar shows
the result when the source changes; it never writes optimistically. Most sources are
not draggable.

**Create.** "Create in…" lists the owning features' own create flows and opens one. An
empty-slot click does the same where the product's own calendar already defines it.

**Time zones.** Instants are stored in UTC by their sources. The calendar displays
**site time** (the site's IANA zone, from site configuration) and labels it on every
view ("site time · CT"). A person away from the site may switch the view to their own
zone; walls and kiosks always show site time. Date-only items (an expiry date, a
closure) are site-local dates, not instants, and do not shift with the viewer. The
daylight-saving change is shown on its day (a 23- or 25-hour day, the repeated hour
labelled). A model's epoch, if a product declares one in a zone, is stated beside the
site zone. A chart opened from a calendar item keeps its own UTC axis and says so.

**Recurrence (display only).** Sources may declare RRULE recurrence; the server expands
it inside the visible window (bounded) with exceptions. High-frequency recurrence
(hourly jobs) collapses to one line with counts ("hourly · 14 of 24 ✓"). Editing a
series stays with the owner.

**Conflicts and overlaps.** Overlapping items lay out side by side. A **conflict** is
an overlap on the same resource that the source's rules (or the resource's capacity)
forbid; it is outlined in the caution colour with "? overlaps X" in words. The calendar
reports; the owner decides.

**ICS.** Export any view as a read-only `.ics`. Subscribe by a per-person secret URL
that the vault issues with an expiry and can revoke; the feed applies the same
visibility as the page, so a limited item exports as "booked" only.

**Deliberately not built.** Invitations, attendees and RSVP; email or external calendar
sync (the scheduler's calendar connector owns two-way sync, separately); free/busy
across people; booking rooms or resources outside a feature that owns them; creating
or editing events in the calendar; editing recurrence; per-person calendars and
colours; reminders configured in the calendar (notifications own reminders); a year
view; appointment slots; tasks. A **program tracker's** schedule is a future source,
not designed here.

### 22.3 The item contract: `ScheduledItem`

Any extension contributes items by implementing one read: `list(window, scope,
viewer) → ScheduledItem[]` (and `show(id)`), declared in its manifest. Field names
follow the scheduler's `EventSpec` and time slots where they overlap.

| Field | Meaning |
|---|---|
| `id` | stable, source-scoped (`<source>:<kind>:<record id>`) |
| `source`, `kind` | the contributing extension and its record kind |
| `title`, `status_line` | short label; one line of state in the source's own words |
| `owner` | principal `@name:context` accountable for the record |
| `timing` | one of: **span** (`start`, `end`; `end` may be open while in progress), **due** (`at`, an instant shown as a marker), **all-day** (`date_start`, `date_end`, site-local dates) |
| `actual_start`, `actual_end` | when the source records them (planned against actual) |
| `timezone` | the source's zone, a display hint; instants are UTC |
| `rrule`, `exdates` | recurrence for display |
| `status` | `tentative`, `planned`, `in_progress`, `done`, `missed`, `cancelled`, `held` |
| `status_label` | the source's own word (a frozen product's terms are kept) |
| `resource` | `{key, label, group}`: what the item occupies; rows of the timeline |
| `visibility` | who may read it, decided by the one authorization decision function (RBAC and ABAC); and `detail`: `full`, `limited` (fields the source names) or `busy` (only "booked") |
| `link` | the owner's deep link (opens its drawer) |
| `move` | absent, or the source verb a drag calls |
| `conflict_rules` | optional, per resource |
| `provenance` | source record version and updated time |

**Status palette (v12).** tentative: caution, dashed border, ⏳. planned: neutral.
in progress: information, ●. done: muted, ✓. missed or failed: alarm, ✕. cancelled:
struck through, hidden by default. held: hatched neutral (data held for quality). A
due marker is ⚑, caution inside its warning window, alarm when overdue. The source is
named by a word tag, never by a colour; the accent marks only today and controls. A
frozen product palette maps onto these (for example its "approved" to the done-or-ok
hue) and keeps its legend words.

### 22.4 Sources (generic)

| Source | Kind | Timing | Notes |
|---|---|---|---|
| The scheduler: schedules and fire log | job runs, next fire | span or due; RRULE / cron | outcome from the fire log; time slots with `resource_key` |
| Time slots (the consumer seam) | reserved resource time | span, planned and actual | the native source for bookings |
| Attest obligations | due dates | due (`due_at`, `warn_at`) | **only obligations measured in days or longer**; minute-scale obligations (a periodic check every half hour) are a countdown, not a calendar item: drawn as events they would be noise, and a calendar would invite "I'll see it" for something whose miss is handled by the obligation engine |
| Vault (KEEP) expiry | credential and grant expiry, rotation due | due or all-day | `rotation_due_at`, `expires_at`, `review_by` |
| Qualifications (Team, v13) | expiry, requalification | due | from the roster's qualification source |
| Roster and shifts (Team, v13) | shifts, on-call | span; shift end as due | rows by role or person on the timeline |
| Product workflow items | requests, bookings | span, tentative to planned | a product's frozen calendar reads only its own items plus "other use" on its resources |
| Site loops (Automate) | maintenance, calibration, surveillance | recurring due or span | from the site's loops |
| Data quality | held windows | span, `held` | past only; links to the hold |
| Publications (public site) | publication times, public events | due or span | opt-in to the public site |
| Closures and holidays | site closed | all-day | site configuration |

### 22.5 Placement

| Option | For | Against |
|---|---|---|
| (a) a Calendar rail item | one obvious home | a rail item for a view that owns nothing; duplicates every owner; adds to a rail the standard holds at a fixed length (ADR-223) |
| **(b) no rail item: schedule slots where the work is, plus one full page** | the schedule appears beside the work it belongs to; the owner said "link to it, and show a week, day or month in certain cases" | discoverability of the full page |
| (c) both | — | the cost of (a) for little gain |

**Recommendation: (b).** The full page lives at `/calendar` and is reached in at most two
clicks from anywhere: from any schedule slot ("Open calendar ↗", one click), from
**More › Calendar** in the rail (two), from Search and from Chat ("what's on this
week" answers inline and links). A persona preset may promote Calendar into the rail
(the same mechanism that hides or shows Model); that is configuration, not a second
design.

| Where | View | Sources |
|---|---|---|
| A product's frozen workflow tab | its own calendar (week grid; by-resource timeline) | its items plus other use on its resources |
| Site › Operate | "next hours" slot (agenda) and "today" slot (timeline) | roster (shift end), planned operations, product items |
| Site › Automate | executors and loops (timeline) | scheduler, loops |
| Model | pipeline schedule (timeline, rows = executors) | scheduler |
| Team | the roster as a timeline (rows = roles) | roster, qualifications |
| Log | a past month: what happened, linking to entries | actuals of all sources |
| "Needs you" | due soon (agenda, 14 days) | obligations, expiries, qualifications |
| Wall displays | the week ahead, and a product's now / next / later | read-only, roles never names |
| Public site | public events and publication dates | opt-in, static |
| Phone | agenda is the default; week is three days wide | all, filtered |

**Timeline (Gantt) belongs** where a resource is occupied for a span: positions or
stations over a week, the plant's operating time, instruments, executors, and people
on shift. Elsewhere the agenda and the grids are clearer.

### 22.6 Phasing

- **MVP (P1): the read only.** The `ScheduledItem` read over sources that already exist
  in the platform (scheduler, obligations, vault expiry), `axi schedule agenda`, the
  MCP tools and the chat answer, and "due soon" inside "Needs you". **No calendar
  component** in the MVP: nothing in P1 needs a grid.
- **P4: the component.** appkit `Calendar` (day, week, month, agenda, timeline) and
  `ScheduleSlot`, because P4's own screens are the first that need them: Operate's next
  hours and today, Automate's executors, the walls' week ahead, the roster. ICS export.
- **P5: the product's calendar tab.** The frozen workflow item's calendar renders
  through the component's week mode (lifted from its prototype) and its by-resource
  timeline; drag calls the product's propose verb. P5 is where a frozen design proves
  the component faithful.
- **P6: Model's pipeline schedule slot.** **P7: phone agenda, ICS subscribe (vault-issued
  URL), public dates on the canvases.**

### 22.7 Open questions

1. Placement (b) with More › Calendar, or a rail item for some personas by default?
2. Which fields a `limited` item shows for a product's requests read by another group
   (the product decides; default: resource, time, status, group, and any sharing flag).
3. Week starts Sunday or Monday: a site setting (default Sunday, as the as-built
   prototype)?
4. The CLI noun: `axi schedule list` already lists scheduler definitions, so the item
   read is `axi schedule agenda` and `axi schedule item show`; or a new noun
   `axi calendar`?
