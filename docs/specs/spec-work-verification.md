# Tech Spec: Work Verification (feature 1)

**Status:** Living. **PRD:** `prd-work-verification.md`.

## 1. Shape

New builtin `verification/` (purpose-named):
- `claims.py` — the five claim types as frozen dataclasses; a claim is
  constructed ONLY from structured observations (tool-call records,
  dispatch envelopes), constructor-enforced (no free-text factory).
- `bindings.py` — one effect binding per claim type, the fleet
  `evaluate_report` pattern (status + cited evidence; can-fail tests
  per binding):
  - `tests_pass`: runner artifacts (junit/pytest reportlog/exit code)
    read from disk, never stdout parsing of prose; counts cited.
  - `files_changed`: path exists + content hash vs claimed.
  - `command_succeeded`: recorded exit status of the dispatched call.
  - `service_restarted`: platform-appropriate probe (launchd/systemd
    state or port probe), OS-adapter seam (macOS + Omarchy first;
    Windows service adapter later, seam present from day one).
  - `deploy_live`: HTTP(S) probe with expected marker.
- `receipts.py` — task receipt = ordinary action-ledger receipt with a
  `claim` + `verification` payload; spawn-tree linkage via the
  dispatch envelope's parentage.
- `session.py` — session-end assembly: gather the session's claims,
  run bindings, emit one summary receipt.

## 2. Attachment seams (reuse ledger)

| Seam | Exists | Use |
|---|---|---|
| Dispatch chokepoint (#941 telemetry point) | yes | observe tool calls → claim candidates; spawn goals/scope capture (PRD R5) |
| Action ledger / `_authz.action` | yes | receipt storage + query |
| Fleet evaluator pattern + Status taxonomy | yes | bindings + VERIFIED/UNPROVEN/CONTRADICTED mapping (VERIFIED=green, CONTRADICTED=failed) |
| Claude Code hooks / OpenCode hooks | external, stable | session-end trigger on the two launch harnesses |
| Receipts surface (F0) | program | rendering: task receipts are EvidenceRows; feed entries on CONTRADICTED |
| `axi did` CLI verb (thin, ADR-056) | new | query receipts: `axi did --today`, `axi did tests_pass` |

## 3. Cross-platform posture (PRD R6)

Pure-Python core; OS-touching bindings go through a small
`os_adapters.py` with mac/linux implementations at launch and a
declared Windows seam (no `systemd`-assumptions outside the adapter).
Hook installers per harness live under `verification/hooks/` with
per-OS install paths (launchd plist / shell profile / Omarchy plugin
hook later).

## 4. Honesty mechanics

- Coverage stat is first-class: `verifiable_claims / total_observed`
  rendered wherever receipts summarize (PRD strawman #4).
- A binding that cannot run (missing artifact, unreachable probe)
  yields UNPROVEN with the missing evidence named — never silently
  skipped (the fleet microcopy pattern).
- CONTRADICTED requires positive evidence against; absence is always
  UNPROVEN.
- Bench: a work-claims corpus extends `axiom.bench.reports_vs_reality`
  (same honesty constraints: healthy control, shipped bindings only).

## 5. Phases

- **P1**: claims + bindings for `tests_pass`, `files_changed`,
  `command_succeeded` with can-fail tests; `axi did`; dogfood on our
  own sessions.
- **P2**: chokepoint observation + spawn provenance; session-end
  hooks (Claude Code, OpenCode); Receipts-surface rendering.
- **P3**: `service_restarted` + `deploy_live` bindings via adapters;
  coverage stat; bench corpus; receipt-post artifact (the "said done,
  here's the receipt" clip).
