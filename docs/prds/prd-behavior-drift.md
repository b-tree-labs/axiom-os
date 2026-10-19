# PRD: Behavior Drift Sentinel (program feature 3)

**Status:** Living. **Spec:** `spec-behavior-drift.md`.

## Problem

The largest single signal in the market corpus: silent model/harness
degradation — 3,287 reactions on one issue, "it degraded by a
generation," "this cost me a day of work," and a third-party nerf
tracker existing because no vendor answers. The vendor's implicit
claim — "the model is the same as yesterday" — is the one claim in the
stack nobody can verify. This is the third altitude of the receipts
thesis.

## Users

- An individual whose daily driver changed under them.
- A team that pinned a model and needs evidence it stayed pinned in
  behavior, not just in name.
- A buyer in a vendor dispute who needs receipts, not vibes.

## Requirements

### R1 — Private probes, personal baselines
A pinned (model, harness) pair gets a PRIVATE probe suite run on
cadence. No shared public suite: shared probes get gamed and leak;
private probes measure what THIS user depends on. Starter packs ship
(code, reasoning, format-adherence, calibration) but the suite is the
user's.

### R2 — Statistical honesty or nothing
DRIFTED is a statistical verdict, not a point diff: paired comparisons
against the pinned baseline window (the calibration/McNemar machinery),
minimum probe counts, effect-size thresholds. Below the bar renders
NOISY — a distinct state, face-up — never DRIFTED. Crying wolf once
kills this product.

### R3 — A fleet report kind, not a new product
Results push as a `model_behavior` report kind; the console renders it
beside backups and heartbeats ("the console watches your models the way
it watches your backups"). STABLE is rendered value, not silence:
"21 days within baseline, N probes" is a receipt someone shows their
lead.

### R3b — Calibration receipts ride the same rails (ADR-126)
Drift verdicts are typed decisions, and every probabilistic decider
seat the sentinel watches also emits a **calibration receipt**: the
per-seat, per-site stated-confidence-vs-empirical-frequency record
over a declared window, pushed as a report kind beside
`model_behavior`. The reliability record is the baseline's sibling —
same statistical bar (R2's NOISY-vs-DRIFTED discipline applies to
calibration decay too), same STALE-at-3×-cadence rule when outcomes
stop arriving, same components on the console. The graduation
extension's outcome log is the first feed.

### R4 — User-protective posture (binding)
Framing is "know when it changed under you." No leaderboards, no
vendor comparisons, no public aggregates in v1. Receipts are the
user's evidence for THEIR decisions (re-pin, re-eval, escalate to
vendor support with data).

### R5 — Probe spend is budgeted
Probing paid models costs money; probe runs draw from a capability
budget (feature 2) with the burn visible. Local models (the Omarchy
Ollama case) probe free and first.

### R6 — Launch platforms
macOS + Omarchy first (Omarchy's ten-harness, local-model culture is
the natural first audience); nothing OS-bound outside the scheduler
adapters (launchd/systemd now, Task Scheduler later).

## Non-requirements (v1)

- No public drift feed, no cross-user aggregation.
- No root-cause attribution (quantization? routing? we report THAT it
  changed and where, never WHY — we cannot know why).
- No harness-version pinning enforcement (report, don't gate).

## Red-team

**Steelman.** Largest signal; near-total reuse (graduation outcome
log = the spine, canary = probe-on-cadence + attestation, calibration
harness = the stats, fleet console = the surface); the only entrant
with statistical discipline; STABLE receipts create daily value even
when nothing drifts; and it deepens the moat story (receipts for the
one claim nobody else can check).

**Strawman.** (1) Nondeterminism: small probe sets on stochastic
models produce false DRIFTED and the product dies of wolf-crying.
(2) Benign changes (temperature defaults, routing) trigger alarms
users can't act on. (3) Probe cost on paid APIs makes the meter feel
like a tax. (4) Vendor-relations blast radius if marketing drifts
into shaming. (5) "Run your own evals" — sophisticated teams think
they have this.

**Design responses.** (1)→R2 is absolute; NOISY exists precisely so
uncertainty renders as uncertainty. (2)→verdicts carry actionability
copy ("format adherence changed; reasoning stable") and the user
chooses probes for what they depend on (R1) — benign-to-you changes
don't probe-fail. (3)→R5 budgets + local-first + starter packs sized
in the tens, not thousands. (4)→R4 binding + campaign posture rules
already merged. (5)→"your evals run on release day; this runs every
night with a baseline window, paired stats, and a receipt — and it
took one command to set up."

## Success criteria / kill test

Success: 30 days of baselines on our own pins (start accumulating
during feature 1 per the program plan); ≥1 NOISY-vs-DRIFTED
distinction demonstrated on real data; a STABLE receipt used in a real
decision (our own re-pin call counts).
Kill (map): if 30-day dogfood shows drift alerts that never change a
decision, it is dashboard decoration — stop before the marketplace
sees it.
