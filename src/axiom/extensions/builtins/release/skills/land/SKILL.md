# land

Report how open pull requests would batch, which look superseded, which
are ready for a person to approve, and which need a person for another
reason. The estimate is suite minutes saved.

`mode` in the config is warn or propose. `--propose` writes a local
proposal. `--act` lands a green, unprotected, non-draft pull request
with no pending human review and no changes-requested review. A
protected path is held. The report path does not merge, enqueue,
force-push, or post a check.

Before it reports or acts, `land` prints `ci lint` findings as warnings.
`--act` obeys the CI budget state the hourly `ci_sweep` records: at warn
it waits for a batch and refuses blind re-runs; at stop it lands only
release pull requests. A green pull request that is behind its base is
updated only when it lands next.

## Inputs

- `repo` — `owner/name`
- `window_hours` — grouping window (default 6)
- `config` — path to a per-repo TOML file
- `format` — `text` or `json`
- `suite_minutes` — override the learned suite cost
- `propose` — record the proposal locally
- `act` — land an eligible pull request. A protected path is held.

## Output

Batches, families, superseded pull requests, ready pull requests, human
decisions, and `minutes_saved`.

## Config

`.axiom/release-lander.toml` in the checkout, or
`<state>/agents/rivet/lander/<owner>__<name>.toml`. A heartbeat reads
repo names from `<state>/agents/rivet/lander-repos`. Tokens stay in the
host environment (`GH_TOKEN`, `GITHUB_TOKEN`, `GITLAB_TOKEN`).
