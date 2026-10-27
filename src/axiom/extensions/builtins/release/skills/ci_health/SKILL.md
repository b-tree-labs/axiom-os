# ci_health

Read where Actions minutes and money go, and which of them are waste.

Reads only: billing usage, budgets, workflow runs, and a sample of
selection-job logs. It cancels nothing and sends nothing.

## Inputs

- `repo` — `owner/name`, or a comma list. Default: `ci_health.repos`, then the watch list.
- `config` — the lander TOML with a `[ci_health]` table.
- `format` — `text` or `json`.

## Output

- Month-to-date minutes, gross and net cost, the run-rate projection, the
  percent of budget, and the budget state (`ok`, `warn`, `stop`, `unknown`).
- Minutes by repo (billed), and by workflow, event and sampled job (run wall clock).
- Waste signals, each with runs, minutes, share and whether it is over
  `ci_health.waste_share`: `update_branch_reruns`, `superseded_running`,
  `selection_fallback`, `same_sha_flakes`, `chronic_red`,
  `duplicate_main_runs`, `main_push_suite`, `failed_or_cancelled`.
- The superseded runs `ci_sweep` would cancel, and branch-protection lint.

## Selection marker

A job that narrows its tests prints `selection: <selected>/<total> reason=<reason>`.
`reason=affected` is a narrowed run. A full suite with any reason not in
`ci_health.intended_full_reasons` counts as a fallback.
