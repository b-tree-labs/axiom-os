# Model Corral and Twin Runtime PRD

**Product / Feature:** Model Corral (registry of computational models of any kind, including surrogates) and the Twin Runtime (hosts a registered model or surrogate beside its physical counterpart)

**Owner:** Platform owner   •   **Status:** Draft, for review   •   **Last updated:** 2026-11-05

**Vocabulary:** [Twin concepts](../reference/twin-concepts.md) defines every term used here   •   **Decision:** [ADR-235](../adrs/adr-235-model-lifecycle-and-twin-runtime-are-platform-capabilities.md)   •   **Builds on:** ADR-204 (ModSim providers), ADR-162 (watchers), ADR-170 (idle slots), ADR-182 (never down), ADR-197 (one switch runner), ADR-136 (uncertainty)

---

## 1) Elevator Pitch

Any team with a physical system and a model of it can register that model, establish its credibility, promote it, and run it, usually as a surrogate, beside the real system with no downtime when it changes. A consuming domain adds its vocabulary and its credibility rules. It does not build a registry or a runtime.

## 2) Problem / Opportunity

- A model of a physical system earns its value only when it runs next to that system, and the step from "trained" to "serving" is where projects stall.
- The registry that exists lives in one consumer, requires that domain's fields, and has no revision lifecycle. Every other domain would have to rebuild it.
- Nothing hosts a model. There is no selection by operating configuration, watchdog, version report, side-by-side comparison or rollback.
- The parts are already here, and nothing connects them:

| Exists in Axiom | Used here for |
|---|---|
| Durable approval gate (`infra/orchestrator/approval.py`) | The promotion record |
| Watcher primitive, event bus, scheduler | Detecting a new revision |
| `infra/deploy/bluegreen.py`, the switch runner (ADR-197) | The switch sequence, today for services and collectors |
| DAQ `Journal`, `LiveDispatcher`, `Transmitter` | The two output rails |
| `StorageProvider`, manifest validation | Package storage |
| `axiom.uncertainty` | Uncertainty on every served value |
| ModSim engine skeleton (PR #1430, draft, not merged) | Slots, run record, verdict |

## 3) Goals & Success Metrics

- **Primary goal:** one lifecycle for any computational model or surrogate, in any domain, from candidate to served.
- **Proof of generality:** two unrelated domains run the full lifecycle with no change to Axiom code, and no domain vocabulary in Axiom.
- **Proposed targets, to be confirmed by measurement:**
  - A switch of served model drops no output frames.
  - Rollback to the previous revision takes under 5 seconds.
  - A staged revision serves on a connected host within one hour.
  - Every served value names its revision, and no held or repeated value is ever published as a prediction.

## 4) Key Users / Personas

- **Model author:** registers a revision and sees what evidence it still needs.
- **Reviewer:** reads the evidence and promotes, or refuses.
- **Site operator:** sees what is running, what is waiting, and rolls back.
- **Controller developer:** consumes predictions through one stream, on replay and live alike.
- **Domain author:** adds a domain layer without changing Axiom.

## 5) Concepts

Every concept here is domain-neutral. The right-hand columns show how different domains fill it in.

| Concept | Meaning | Process plant | Crop field | Production line |
|---|---|---|---|---|
| **Computational model** | An executable representation of a system | Solver and input deck | Soil or yield model | Cell simulation |
| **Surrogate** | A cheaper stand-in for a higher-fidelity model or for behaviour. The usual thing a host serves | Reduced-order model | Trained yield estimator | Learned quality predictor |
| **Assembly** | A model built from other registered models | Coupled flow and heat | Soil, weather and crop | Machine, buffer and quality |
| **Revision** | One immutable version of a model | `corral:<name>@<revision>` | Same | Same |
| **Package** | What a host needs to run a revision | Same contract | Same | Same |
| **Operating configuration** | Typed variables, with uncertainty, that decide which model applies | Equipment line-up | Field, crop, season | Line, product, recipe |
| **Validation domain** | The conditions where a revision was checked against reality | Same contract | Same | Same |
| **Credibility profile** | The stages and evidence a domain requires before promotion, per context of use | Formal verification and validation | Hold-out seasons and fields | Held-out production runs |
| **Service class** | The deadline a revision declares it meets | Control-rate tiers | Hourly or daily | Cycle time |
| **Placement** | A revision on a host in a role | Same contract | Same | Same |
| **Rung** | Model, shadow or twin: how closely it is tied to the system | Same contract | Same | Same |

A **digital twin** is a rung of integration. It is not a kind of model. A surrogate served at the shadow rung is a digital shadow, and it reaches the twin rung only when a person promotes it onto a named control path. Rungs follow Kritzinger et al. (2018), and credibility follows ASME V&V 40.

## 6) Layers

| Layer | Owns | Must not contain |
|---|---|---|
| **Axiom** | Model Corral, lifecycle, packages, delivery, host, worker protocol, adapters' contract, rails, control envelope, conformance suite | Any domain noun, model library or domain rule |
| **Domain layer** | Kinds and metadata, credibility profile, service-class names, operating-configuration variables, worker adapters for its libraries, verbs | Facts about one site |
| **Site layer** | Configuration values, model inventory, channel maps, envelope limits, drive and actuator bindings | Platform behaviour or UI |
| **Model libraries** | Composition, training, stepping, checkpoints | Hosting, registry or delivery |

The test: remove the domain noun. If a capability remains, it belongs in Axiom.

The host never imports a model library. A worker adapter is the only code that does, and it runs in the worker's process.

## 7) The Lifecycle

| Step | State or role | What happens | Who moves it |
|---|---|---|---|
| 1 | Hypothesis | A claim with its test windows and threshold, fixed before the run | Person or agent, within a budget |
| 2 | Data | A registered, versioned training set with a recorded split | Automation |
| 3 | **candidate** | Training registers a revision, untested, with its lineage | Automation |
| 4 | **verified** | The model is solved correctly, and the package does what it claims | Automation, against the profile |
| 5 | **validated** | Results agree with held-out reality across the validation domain, with uncertainty quantified | Automation, plus any reviewer the profile requires |
| 6 | **staged** | Accepted for named contexts of use. This is the promotion | **A person, on the approval gate** |
| 7 | **deployed, challenger** | Running on a host beside the served revision, judged on the same frames | Placement policy |
| 8 | **deployed, served** | Its outputs are what consumers receive | Placement policy at the shadow rung, a person at the twin rung |
| 9 | **superseded** or **withdrawn** | Replaced, or pulled with a recorded reason | Selection, or any reviewer |

Rules:

- Registry state (steps 3 to 6, 9) is a fact about a revision. Placement (steps 7 and 8) is a fact about a revision on one host. They are separate records.
- Automation can never reach **staged**. A confirmed hypothesis produces a candidate, never a promotion.
- Rollback is the same transition in reverse, on the same record.

## 8) Scope: Requirements

### Model Corral

- **C1:** A revision of any kind registers without domain-required fields. Surrogates are first-class: a revision says which class it is (data-fit, reduced-order, multi-fidelity) and what it approximates. Domain fields live in a namespaced, schema-checked extension.
- **C2:** A revision records its kind, parent, content hash, producer, evidence and state. Lineage is a graph, so an assembly names its member revisions.
- **C3:** A domain declares an credibility profile. Axiom enforces it and does not know what the evidence means.
- **C4:** `resolve(configuration, quantities)` returns one qualified revision, a transition, or a refusal that says what is missing. It never returns a neighbouring configuration's model.
- **C5:** "What depends on this upstream change?" is a query.
- **C6:** Record fields are derived from the artifact where they can be, so a record cannot drift from what it describes.
- **C7:** Existing registry records keep working through migration.

### Package

A package is required. A model file alone cannot run, because its environment must arrive with it, and because loading some model formats runs code named in the file.

- **P1:** A package holds a manifest, artifacts, an environment lock, the input and output declaration with units, the validation domain, service class, control class, and worker adapter id.
- **P2:** Packages are content-addressed and signed. A host verifies both before anything is loaded.
- **P3:** Environments are stored separately and shared between revisions, so a new revision of a large model ships only what changed.
- **P4:** One package format runs in a container or a locked subprocess.

### Detection and delivery

- **D1:** A host declares what it serves. A reconciler compares that with what the registry says is staged for its current configuration, and acts on the difference.
- **D2:** Four sources feed the same event: registry poll, registry event, a watched drop folder, and a signed bundle from removable media.
- **D3:** Detection authorizes nothing. Every source passes the same signature, state and validation-domain checks.
- **D4:** A host inside a protected network only ever pulls. Nothing reaches into it.

### Runtime

- **R1:** One worker process hosts one revision. A worker crash cannot take down the host, and a new dependency stack cannot affect another model.
- **R2:** The host talks to workers through a versioned protocol: load, describe, step, evaluate, checkpoint, health, drain.
- **R3:** Workers never open a database, read a feed or publish rows. The host does those.
- **R4:** A step that misses its deadline publishes nothing, and the stream reports `stale`. Non-finite values are faults.
- **R5:** A dead or stalled worker is replaced from its latest checkpoint and catches up from the journal before it goes live.
- **R6:** The host reports what is running: revision, package hash, environment hash, library versions, health and step times.
- **R7:** The same worker code runs in process for tests and batch replay, and never for a served revision.
- **R8:** The configuration variables come with uncertainty. While the configuration is transitioning or unknown, the host follows the rung's policy and says so.

### Deployment

Stateful models cannot share traffic the way web requests do, so the challenger receives a mirror.

- **B1:** The challenger starts in an idle worker, proves readiness on a replay, then receives the same live frames as the served revision.
- **B2:** Only the served revision reaches consumers. Challenger output is journaled and labelled.
- **B3:** The comparison runs for a declared window against declared criteria, and its result is recorded.
- **B4:** After a switch, the previous revision stays warm for a declared time, so rollback is immediate.
- **B5:** Every switch runs on the ADR-197 runner: prepare, probe, commit, rollback.
- **B6:** A zero-gap upgrade test is required, as for every role that serves (ADR-182).
- **B7:** At the twin rung, a warm standby is required unless restore-and-replay is proven inside the control deadline.

### Two rails

- **T1:** Each output frame fans out to a real-time rail and a durable rail, and neither can block the other.
- **T2:** The real-time rail is a `LiveDispatcher` subscriber, with no storage in its path.
- **T3:** The durable rail is the `Journal`: it survives restart and outage, and an overflow follows a declared policy.
- **T4:** The `Transmitter` ships journaled rows when connected. A new export-bundle transport ships them on removable media.
- **T5:** Predictions and measurements keep separate journals, so a model fault cannot stall measured data.

### Control boundary

- **K1:** A served model can command a real actuator through a named control path. That path exists to do so.
- **K2:** Every command passes a site-owned envelope check and an authority class: shadow, advisory or executing.
- **K3:** Every command is journaled with its inputs, decision, envelope verdict and authority.
- **K4:** The same actuate call drives a simulated or real actuator. Only the adapter changes.

None of K1 to K4 is built today.

## 9) Non-Functional / Constraints

- **Platforms:** Linux container, and locked subprocess on Linux, macOS and Windows. A host cannot assume containers.
- **Footprint:** runs on one small machine, with no cluster and no inbound connection.
- **Offline:** a host keeps serving when disconnected.
- **Security:** signed packages, sandboxed loading, an allow-list for classes a checkpoint may construct.
- **Determinism:** same package, same frames, same outputs on the same hardware, or the deviation is declared.

## 10) Non-Goals

- Training algorithms, and any model library.
- Autoscaled multi-tenant model serving on a cluster.
- Browser-side inference as a serving path.
- Domain evidence rules.

## 11) Timeline

Tracks the ModSim integration plan's phases. Phase 4 is built first on a reference model, so delivery does not wait for Phase 3.

| Step | Plan phase | Done when |
|---|---|---|
| **W1 Core** | 1 | Engine merges. Corral runs in Axiom with lifecycle states and `resolve` |
| **W2 Reference proof** | 4 | A simple reference model goes candidate to served on a laptop: package, detect, challenger, switch, rollback, kill and recover |
| **W3 First domain, site node** | 4 | A consumer's existing model runs through the same path at the shadow rung, beside what serves today |
| **W4 Protected edge** | 4 | The same package serves on a host inside a protected network, by pull or bundle |
| **W5 Training path** | 3 | A training job produces a candidate with its evidence |
| **W6 Second domain** | 4 | An unrelated domain passes the conformance suite with no Axiom change |
| **W7 Twin rung** | 5 | A control path, envelope and authority gate exist, and a person promotes a served model onto one |

## 12) Risks & Open Questions

- **Registry move:** moving Corral's core to Axiom must not stall its current owner's work. The migration is by extraction, in steps.
- **Scope:** the control boundary is large. It is deliberately last, and W2 to W4 do not need it.
- **Model libraries:** their failed-step behaviour, checkpoint compatibility and packaging decide how much the worker does itself.
- **Open:** does the host belong in the node service or run as its own process? Recommended: its own, so a node upgrade does not restart models.
- **Open:** who may approve a switch at the shadow rung? Recommended: policy, with a person notified.
- **Open:** how long does the mirror run by default? Each domain's profile should set this.

## 13) Acceptance & Rollout

- The conformance suite passes with the reference model and every domain's first model, with the same tests.
- The naming guard passes on all new Axiom code.
- Each domain layer is an extension installed on top of Axiom, and removing it leaves the platform working.
