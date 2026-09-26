# Tech Spec: Capability Budgets (feature 2)

**Status:** Living. **PRD:** `prd-capability-budgets.md`.

## 1. Model

- `BudgetEnvelope` (frozen): `unit` ("tokens" | "calls"), `cap`,
  `drawn`, `warn_threshold` (default 0.8), `parent_token_id`.
  Attached to `CapabilityToken` as an optional field — tokens without
  envelopes behave exactly as today (R6: top-level default-off).
- Draws are ledger rows (`budget_draw`: token id, amount, unit,
  surface, spawn id, receipt id) — the burn-down and "where every
  dollar went" are queries, not new state.

## 2. Enforcement (the floor)

`decide()` step 1c (after lifecycle, before policy): if the envelope
exists and `drawn + requested > cap` → `Decision.DENY`, reason
`budget_exhausted`, receipt carries the draw history pointer. Denial
applies to NEW actions only (PRD R4): the executor checks at dispatch
boundaries; nothing in-flight is interrupted.
Metering source: the per-surface telemetry projection (#941) already
counts invocations per surface; token draws ride the same chokepoint
event, so metering adds one field, not one system.

## 3. Spawn inheritance (R2)

Mint-for-spawn takes `slice` (absolute or fraction of parent
remaining); parent's `drawn` is incremented by the slice at mint (the
tree's ceiling is conserved — no double-spend between parent and
child). Delegation depth mechanics unchanged.

## 4. Renewal interplay (R7 — pinned by test)

`capability_factory` re-mints carry the envelope by REFERENCE
(cap/drawn persist in the ledger, not the token object), so renewal
can never refill. Test: age a context past TTL with 90% drawn →
renewed token still shows 90% drawn.

## 5. Warning + raise (R5 + red-team #2)

At threshold crossing: one HERALD `budget.warning` with burn-down
attached (the `_herald.publish_event` pattern — never raises).
`axi budget raise <token> --by 50% --for-task` mints a one-task
increment, receipted with reason; no unbounded raise verb exists.

## 6. Surfaces

- CLI: `axi budget` (`status`, `raise`, `history`) — thin per ADR-056.
- Receipts surface: `BudgetBar` appkit component (burn-down per spawn
  tree; amber at threshold; denial rows in the change feed).
- Desktop glyph (F4): burn fraction as the glyph's secondary
  indicator.
- Dollar rendering: `price_table.toml` (versioned, visible), always
  "≈ est." suffixed (R3).

## 7. Cross-platform

Pure platform logic — nothing OS-touching. The desktop rendering rides
F4's per-OS shells (waybar / macOS menu bar).

## 8. Phases

- **P1**: envelope + floor check + conservation math, TDD (incl. the
  renewal-never-refills and conservation-under-concurrent-spawn
  tests); `axi budget status`.
- **P2**: spawn-slice minting at the dispatch chokepoint; HERALD
  warning; dogfood ON for our orchestrator + session spawns.
- **P3**: BudgetBar + feed rendering; raise verb; price table;
  governance-overhead bench extends to the draw path (the cost of
  metering must itself be measured).
