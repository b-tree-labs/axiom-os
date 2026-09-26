---
name: graduation.status
description: Report the shadow outcome log for the tier classifier — switch, phase, backend, record counts, log path. Read-only.
allowed-tools: []
---

# graduation.status

Report the state of the shadow-mode outcome log the graduation extension
accumulates for the LLM tier classifier.

## Params

None.

## Returns

`value` dict:

- `switch` (str) — the shadowed switch name (`llm_tier_routing`).
- `phase` (str) — lifecycle phase (`RULE` in phase 1; the deterministic
  rule is and stays the decision-maker).
- `enabled` (bool) — whether shadow recording is on for this process.
- `backend` (str) — `postrule` when the optional SDK is installed,
  `jsonl` for the built-in locked-append fallback.
- `log_path` (str) — absolute path of the active outcome-log segment.
- `records` (int) — total outcome records (rotated segments + active).
- `outcomes` / `labels` (dict) — counts by verdict and by chosen tier.
- `first_timestamp` / `last_timestamp` (float|null) — record time range.

## Behavior

Read-only: loads the JSONL outcome log and summarizes it. Never writes,
never contacts any service, never reads classified content — the log
holds decision shapes only.
