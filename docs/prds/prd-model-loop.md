# PRD: the model improvement loop

**Owner:** platform • **Status:** Draft, design round 1 • **Last updated:** 2026-10-30
**Related:** [prd-site-view.md](prd-site-view.md) (the improvement loop as a Site loop),
[spec-modsim-engine.md](../specs/spec-modsim-engine.md) (#1430, slots, run record, verdict),
[ADR-204](../adrs/adr-204-modsim-providers-register-through-an-entry-point-and-the-control-gate-is-the-executors.md),
[ADR-128](../adrs/adr-128-medallion-tier-boundaries.md) (tier boundaries),
[spec-webapp-buildout-p6-model.md](../specs/spec-webapp-buildout-p6-model.md) (P6)

## Why this exists

A site that runs a digital twin needs the twin to get better, not just to run. Today the
work that improves a surrogate (change an input, run a simulation, train a new model,
check it, put it in service, watch it, decide what to try next) happens in scripts,
terminals and people's heads. Nobody can see where a pass is stuck, why a served model
exists, or whether the loop is getting faster.

This PRD makes that loop a platform capability: a set of records, verbs on every surface,
and one view of the cycle. It is domain-agnostic. A product supplies the codes, the
spec families and the model kinds; the platform supplies the loop.

## The loop

One pass (a **cycle**) runs through twelve stages:

spec → input deck → run → output → train → V&V → register → deploy → monitor →
analyse → hypothesis → stage the next cycle.

A stage is done when its contract is met, shown by a record, not when someone says so.
Each stage has one owner of its contract. A hypothesis with a test fixed in advance opens
the next cycle.

## Nouns

Each noun has a stable id plus a content hash. Anything a later number depends on is
immutable once used.

| Group | Noun | What it is | Lifecycle |
|---|---|---|---|
| Build | **Spec** | a code-agnostic description of the system; the human-auditable form an agent may write | draft → reviewed → rendered |
| Build | **Input deck** | the file a code consumes, rendered from a spec or brought as written | catalog revision; immutable |
| Build | **Code** | a simulation code pinned to a version and build, with where it may run and under what control | registered → contract-tested → verified |
| Build | **Executor** | where a job runs: local, container, HPC batch queue | available · unavailable · refused by the control gate |
| Run | **Run** | one execution, scheduled into a window or submitted now; signed run record | requested → scheduled → queued → running → collected → landed · faulted · failed · refused |
| Run | **Dataset** | a run's landed tables, conformed to gold and tagged (purpose, regime, configuration, hypothesis, basis) | bronze → silver → gold; frozen when used |
| Learn | **Training set** | a frozen selection of gold datasets and measured windows, split train/test with a leakage guard | draft → frozen |
| Learn | **Training run** | a run in the `train` slot that fits a model kind | as a run |
| Learn | **Model kind** | the interface every surrogate family fills | registered → contract-tested → verified |
| Learn | **Surrogate revision** | a trained model of any kind, keyed to an operating configuration, with a validity envelope | three axes (below) |
| Trust | **V&V record** | verification and validation checks with evidence and a verdict per check | open → assembled → verdict → signed → revoked |
| Trust | **Promotion** | the signed decision that changes a revision's state or rung | asked → signed · held · refused; reversible |
| Trust | **Deployment** | a staged, checksummed package on a serving host | staged → serving → withdrawn · rolled back |
| Watch | **Shadow and monitor** | the standing object for a served revision and its residual monitors | healthy · degraded · refusing |
| Watch | **Residual set** | measured minus predicted, per signal, regime and channel | live view → frozen |
| Watch | **Analysis** | a bounded investigation with a notebook | open → concluded · abandoned |
| Watch | **Finding** | a named weakness or opportunity with evidence | open → triaged → hypothesis · dismissed |
| Improve | **Hypothesis** | claim, change (a diff to a catalog revision), regime, test, parent, origin, budget, verdict | proposed → accepted → running → confirmed · refuted · inconclusive |
| Improve | **Cycle** | one pass: stages, owners, blockers, budget spent, outcome | open → closed (promoted · kept the parent · abandoned) |
| Improve | **Pipeline** | the template a cycle follows: stages, providers, gates, owners, cadence | draft → active → retired |
| Surface | **Notebook** | an agent notebook attached to an analysis, a hypothesis or a V&V record | draft → frozen |

Referenced, not owned: the operating configuration and its key, the measured feeds, the
site's experiments, quality marks, people and grants.

### One catalog

Specs, decks, codes and surrogates live in **one catalog** (the product's model registry,
over `ArtifactRegistry`'s content addressing). "Decks", "codes" and "surrogates" are
views. The engine's provider registry is canonical for what can run where and under what
control; the catalog is canonical for the exact build a run cites. Training-data
versioning belongs to the catalog over gold; there is no second data-versioning store.

### Three axes of standing

A revision's standing is three independent axes, all shown on every screen and derived
from records, never typed:

- **State:** candidate → accepted → superseded · withdrawn. Moves only by a signed promotion or demotion.
- **Evidence:** registered → contract-tested → verified → validated (per regime). Read from V&V records; revocable.
- **Rung:** model → shadow → twin. A model moves data by hand; a shadow receives measured data automatically, one way; a twin's outputs change the physical system through a named control path, inside an approved operating envelope.

"Staged" and "deployed" are states of the deployment, not of the revision.

### The model-kind interface

Every surrogate family fills one interface, so the loop never branches on the kind:

- **declare:** input features, predicted features, configuration key, validity envelope;
- **train:** from a frozen training set and stated parameters or a search space;
- **predict:** from a loaded artifact, for a configuration;
- **validate:** supply the checks the kind needs beyond the common ones;
- **serialize / load:** one artifact with a content hash.

A kind is a learner provider in the engine's `train` and `predict` slots. Catalog metadata
is derived from the declaration, not typed beside it.

## Verbs

Each verb is one skill function (ADR-056) projected to the page, the CLI (`axi modsim …`
for engine verbs; a product adds its own noun for the catalog), chat and MCP. Two verbs
have no MCP tool by design: **sign a V&V record** and **approve a promotion** are a
person's acts on the one signing card. An agent may ask, prepare and explain.

| Noun | Verbs |
|---|---|
| spec | draft from a request, check, render |
| deck | list, show, diff, lineage, add, fork, lint |
| code, executor | list providers and levels, plan (dry run) |
| run | schedule into a window, run, status, cancel |
| dataset | identify, tag |
| training set | freeze |
| training run | train (kind as a parameter) |
| surrogate | list, compare |
| V&V record | open, run checks, show, sign (person only) |
| promotion | ask, approve or demote (person only) |
| deployment | stage, roll back, which revision served at time T |
| shadow, residual set | list, status, compare, history, read |
| analysis, finding | open, raise, triage, dismiss |
| hypothesis | propose, accept (freezes the test; person only), rank by expected gain per compute unit, close |
| cycle, pipeline | open, show, close, history; show and edit the pipeline |
| notebook | run, replay, export |

## The pipeline view

The cycle has two views: stepper (default) and loop. The stepper lists one stage per row with a progress bar and a matching side rail; the loop is the product's loop graphic made interactive. The choice is remembered per person, and the blocking ask stays visible in both.

The product's model page lands on the **current cycle**: twelve stages with state (glyph
and word), owner and the one thing blocking; what serves now and what waits behind it with
all three axes; and what needs the viewer. The same cycle strip appears on a hypothesis and
in Site › Automate › Loops. Cycle history lists generation N → N+1 with hypothesis,
change, verdict, outcome, cycle time and compute, and lineage runs backward from any
prediction and forward from any hypothesis.

## Notebooks

**Decision (recommended):** agent notebooks are the working surface; Jupyter is a file
format, not a kernel on the node.

- Cards are registered tool calls or Python, with input and output hashes; replay gives
  the same run record.
- Agent cards run through the same skill functions as chat, under the caller's grants.
  Python cards run only on an executor; the control gate decides which. No code runs on
  the serving node.
- `.ipynb` is import/export (export writes cards as cells that call the CLI or Python
  client, with a provenance header; imported cells are untrusted until run on an executor).
- Outputs inherit the strictest classification of their inputs.
- Attach points: an **analysis** (exploration), a **hypothesis** (the test, frozen at
  acceptance), a **V&V record** (the evidence, replayed into the signed package).

The notebook itself (cell kinds including chat cells with action cards, AI provenance,
execution, teaching notebooks, and what promoting a notebook produces) is specified in
[prd-notebooks.md](prd-notebooks.md).

**Learning a cycle by rerunning it.** Any past cycle offers "Learn from this cycle": a
fork into a practice sandbox with the same spec, deck, code revision and data snapshot,
stepped through with a guided tour, in three modes: replay (recorded outputs, no compute),
re-run small (declared reductions, estimated cost) and re-run for real (the original
cost, the person's allocation). Practice cannot promote, register, deploy, sign or write
to the real registries, gold or logs; graduating copies the learner's change into a new
draft cycle at stage 1. An empty site's Model page offers "Start by rerunning a sample
cycle".

**Fallback:** an analysis attaches an `.ipynb` as a Library file run in the person's own
HPC Jupyter session, with outputs imported as artifacts. Nothing executes in the app.

## Ties to the platform

| Piece | What the loop does there |
|---|---|
| Log › Change | promotions, demotions, deployments, rollbacks, served-revision changes |
| Log › Model (proposed type) | runs, training, verdicts, hypotheses, cycles, findings; qualification retention |
| Data quality | held, stale or bad windows are refused from training sets and monitors, and counted; a later mark revokes affected evidence |
| ConCert | V&V evidence packages certify "fit for its regime"; a deployed surrogate shows *certified through*; drift, an upstream change or a lapsed check ends it |
| Signing | Sign · Hold · Ask on the one signing card; one record through the durable approval gate |
| Site › Automate | the served revision is a knob; at the twin rung the loop lists its control path and envelope; the improvement loop is listed under Loops |
| Site › System | the surrogate's place in the control structure: the estimator / model selector |
| Chart | residual and monitor views are Chart catalog items in chart slots; no separate chart screens |
| Library | specs, decks, reports, papers, exported notebooks |
| Needs you, notifications | asks, signatures due, failed runs, findings to triage, degraded monitors, certification ending |
| Scheduler | runs into windows; cycles triggered by time, event (configuration change) or threshold (drift) |
| Data platform | outputs land through the one ingest door; serving and training read gold only |
| Access | classification attributes on codes, decks, outputs and notebooks; the executor's control gate decides where a job runs |

## Guardrails

- Promotion is always a person's act.
- A hypothesis's test is frozen at acceptance and never used for tuning; thresholds are stated in advance.
- Compute is spent only inside a budget a person granted.
- Training, monitors, analyses and charts read gold, never silver.
- Every predicted row carries `model_ref`, the run record and the hypothesis id.
- Controlled codes and decks run only on executors holding the authorization.
- A run that completed is run evidence, never validation; a check with no accepted reference reports "no reference" and cannot pass. (Adopted from a domain product's surveyed colleague work.)
- Executors report status without blocking and treat job arrays as one run; a run stages its outputs back and records what landed even when a step fails.

## Acceptance

| ID | Acceptance |
|---|---|
| ML-01 | A cycle can be opened from a finding, carried through all twelve stages, and closed, entirely from the page, and the same steps run from CLI and MCP |
| ML-02 | Any surrogate revision shows state, evidence and rung, each derived from records |
| ML-03 | A training set refuses windows carrying held or stale marks and reports the count |
| ML-04 | An accepted hypothesis's test cannot be edited; a changed test is a new hypothesis |
| ML-05 | Promotion and V&V signature are unavailable to agents over MCP |
| ML-06 | A notebook replays deterministically; Python cards never execute on the serving node |
| ML-07 | Lineage reaches from any prediction back to the residual that started its cycle |
| ML-08 | Loop metrics (twin quality, loop speed, automation burden, operations) are gold views |

## Open questions

1. Findings: reuse attest's findings record with a *model* scope, or a separate record? (Recommended: reuse.)
2. A Model log type beside Change, or everything under Change? (Recommended: a Model log.)
3. Calendar windows versus submit-now-within-budget as the default for HPC runs.
4. How held-out windows are retired once used as a test, so they never leak across cycles.
