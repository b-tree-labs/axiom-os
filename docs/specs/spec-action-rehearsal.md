# Tech Spec: Action Rehearsal (feature 6)

**Status:** Living. **PRD:** `prd-action-rehearsal.md`.

## 1. Shape

New builtin `rehearsal/`:
- `twins.py` — `TwinProvider` protocol: `snapshot(target) -> TwinRef`,
  `apply(twin, commands) -> TwinRun`, `diff(twin) -> Evidence`,
  `teardown(twin)`; every TwinRef carries `recorded_at` (PRD honesty:
  age is always rendered). v1 providers:
  - `PostgresForkProvider` — reuses the backup scratch-restore
    machinery (`scratch_dsn_for`, `_restore_into_scratch`) verbatim;
    diff = row-count/schema delta.
  - `SnapshotFsProvider` — container/overlayfs (Omarchy/linux) and
    APFS clone (macOS) adapters; diff = file manifest delta.
  - `CassetteHttpProvider` — record/replay proxy for HTTP targets;
    "diff" = request-stream comparison vs recording.
  - `DryRunProvider` — wraps tools with native `--dry-run`/`--check`
    (kubectl, terraform, rsync), capturing their plan output as the
    evidence.
- `episode.py` — a rehearsal episode: take the dispatch record's
  command stream (F1's structured observations — same source, R2),
  apply to twin, run PoD bindings inside the twin, emit ONE receipted
  episode (twin ref + age, commands, diff, verdict).
- `authority.py` — per-target authority ladder (shadow/propose/act)
  stored beside the target's config; promotion verb receipted;
  pinned-floor list (action classes that never auto-act) in one
  visible config. Enforcement at the dispatch chokepoint: an action
  on a laddered target consults the ladder BEFORE execution (this is
  the one place rehearsal gates — per the ladder the human chose).
- Approval rendering: the ApprovalGate (existing DOORBELL flow) card
  gains the episode receipt attachment (PRD R3).

## 2. Reuse ledger

| Reused | For |
|---|---|
| backup scratch-restore machinery | the Postgres twin (near-verbatim) |
| dispatch records (F1 observations) | the command stream to replay |
| PoD bindings (F1) | in-twin verification |
| ApprovalGate + RACI | propose-level flow + receipts on approvals |
| action ledger | episode receipts |
| Receipts surface | episode + approval cards; `RehearsalCard` appkit component |
| fleet taxonomy | episode verdicts reuse Status semantics |

New: providers, episode assembly, authority ladder, RehearsalCard.
Per-target twin cost budget (PRD strawman #3): each provider's spec
row states its one-command setup or it doesn't ship in v1.

## 3. Cross-platform

Provider adapters per OS (APFS clone vs overlayfs); cassette + pg +
dry-run providers are OS-neutral. Windows: VSS-snapshot adapter named
as the later seam.

## 4. Phases (after F1 — the command stream and bindings are its
outputs; sequencing per program rule of one feature in flight)

- **P1**: TwinProvider protocol + Postgres + dry-run providers;
  episode receipts; shadow mode on our own deploy/migration actions.
- **P2**: authority ladder + ApprovalGate attachment + fs snapshot
  provider; the approval-with-evidence card in the Receipts surface.
- **P3**: cassette provider + physical-protocol cassettes; the
  campaign artifact (a rehearsal catching a real would-have-failed
  run — the receipt tells it).
