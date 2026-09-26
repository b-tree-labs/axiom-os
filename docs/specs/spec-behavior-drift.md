# Tech Spec: Behavior Drift Sentinel (feature 3)

**Status:** Living. **PRD:** `prd-behavior-drift.md`.

## 1. Shape

New builtin `drift/`:
- `probes.py` — `ProbeSpec` (id, prompt or callable, scorer, expected
  band), `ProbeSuite` (versioned, hash-pinned; edits create a new
  suite version and a new baseline epoch — comparisons never span
  suite edits). Starter packs as data files.
- `runner.py` — executes a suite against a (model, harness) target via
  the LLM gateway; per-run attestation record (the canary
  `CanaryAttestation` shape, reused with probe results as
  smoke_results).
- `stats.py` — paired comparison against the baseline window: McNemar
  for pass/fail probes, bootstrap CI for scored probes; verdict ∈
  {STABLE, NOISY, DRIFTED} with N, effect size, and the diverging
  probe ids cited. Thresholds in one visible config with defaults
  (min_n=30, α=0.05 with correction, min effect size).
- `report.py` — pushes a `model_behavior` fleet report:
  `{target, suite_version, verdict, n, effect, diverging: [probe ids],
  baseline_epoch}`; fleet `status.py` gains the binding (GREEN=STABLE
  w/ receipts, UNPROVEN=NOISY, FAILED=DRIFTED, STALE by cadence as
  everywhere).

## 2. Reuse ledger

| Reused | For |
|---|---|
| graduation outcome log pattern | run/outcome persistence |
| canary attestation + GossipSink→fleet push | run records + transport |
| calibration harness (postrule) stats | paired tests, CI machinery |
| PULSE schedules | cadence (nightly default) |
| LLM gateway + tier policy | target invocation (one seam for all providers/local) |
| capability budgets (F2) | probe spend (PRD R5) |
| fleet ingest/report kinds | one new kind, zero new transport |
| Receipts surface | rendering; `DriftTimeline` appkit component (baseline band + nightly points + verdict pills) |

New: probes/stats/report modules; the DriftTimeline component; scheduler
adapters only via existing PULSE (launchd/systemd already handled at
enrollment level; Windows later).

## 3. Baseline mechanics

Baseline epoch = first K runs (default 7) after (target, suite
version) pinning; rolling window thereafter for the null distribution.
Verdicts always name their epoch. Re-pin (user action, receipted)
starts a new epoch — drift is measured against what YOU pinned, not
against history you disowned.

## 4. Honesty mechanics

- NOISY is a first-class state everywhere (fleet taxonomy gains
  nothing new — it maps to UNPROVEN's "claim without sufficient
  evidence" semantics, with drift-specific copy).
- The bench gets a synthetic-drift corpus (inject known deltas; assert
  DRIFTED fires above threshold and NOISY below) — the can-fail suite
  for the stats themselves.
- Verdict copy carries actionability (which probe families moved).

## 5. Phases

- **P1** (starts during program feature 1, collection only): probes +
  runner + nightly PULSE run against our own pins (local models
  first); attestations accumulate; NO verdicts rendered yet.
- **P2**: stats + verdicts + synthetic-drift bench; `axi drift status`.
- **P3**: fleet `model_behavior` binding + DriftTimeline in the
  Receipts surface; budgeted paid-model probing; receipt-post artifact
  (a real STABLE-streak or caught-drift screenshot).
