# hygiene.artifacts

Inventory GitHub Actions artifacts and caches, apply TIDY's storage policy,
and delete inside that policy by default.

## Default is agency, with a safe envelope

Like RIVET, TIDY *acts* inside policy. The scheduled tick and
`axi hygiene artifacts` delete superseded `dist`, stale CI logs, and
merged/closed-PR caches unless:

- the kill switch is on (`hygiene.storage_enabled=false` or
  `AXIOM_HYGIENE_STORAGE_ENABLED` in `0/false/off/no`)
- `hygiene.storage_watch_only` is true
- the caller passed `--dry-run`
- the surface is MCP / agent_tool without an explicit `enforce`

Every delete is bounded by a per-run blast-radius cap (default 200 items
/ 2 GiB; 500 / 4 GiB when usage is ≥85%), a rate limit (default 4 enforce
runs/hour), and a hard never-touch list. Every action writes an audit
record, posts a HERALD summary of what/why, and names a recovery path
(re-run the source workflow to regenerate a dist; caches rebuild on the
next CI run).

`axi hygiene stat storage` and the weekly digest stay report-only.

## Policy (autonomous)

Retention is per class (settings `hygiene.retention.*`, env `AXIOM_RETENTION_*_DAYS`,
and `agents/tidy/policy.toml`). Defaults:

| Class | Default | Notes |
|-------|---------|-------|
| main dist | 3 days | Newest 2 kept regardless of age |
| PR dist | 3 days | |
| test results / logs | 14 days | |
| coverage | 14 days | |
| release dist | 14 days | Latest version-tag dist is never-touch |
| benchmarks | keep | `memory-benchmarks*` |

Workflow `retention-days` is the GitHub ceiling, driven by repo variables
with the same fallbacks: `vars.AXIOM_DIST_RETENTION_DAYS` (3),
`vars.AXIOM_RELEASE_DIST_RETENTION_DAYS` (14), `vars.AXIOM_LOG_RETENTION_DAYS` (14),
`vars.AXIOM_COVERAGE_RETENTION_DAYS` (14), `vars.AXIOM_BENCHMARK_RETENTION_DAYS` (90).
Raise the variable if RIVET must keep an artifact past the class default.

- Delete Actions caches whose `refs/pull/N/merge` pull request is merged or closed.
- Never delete caches on `main` or open PRs, in-flight main dists (newest N), the latest
  tag dist, memory-benchmarks, or packages.

## RIVET disposability

RIVET writes a per-artifact/run verdict (`keep` / `disposable` / `unknown`, a
reason, and `expires_at`) to `<state_dir>/agents/rivet/artifact-verdicts.json`
and may emit `rivet.artifact_verdict` on the event bus. TIDY consults it
before deleting:

- `keep` — skip, even if past retention.
- `disposable` — eligible early, still inside caps and never-touch.
- `unknown`, expired, or an unreachable store — retention only; never delete early.

RIVET's reason is copied into the audit log and the HERALD note.

## Thresholds

At 70% of the included shared cap, TIDY reclaims in-policy and alerts.
At 85%, it reclaims aggressively (still inside policy and caps) and raises
a Needs you item if storage is still over after the run.

## Usage

```
axi hygiene artifacts --repo owner/name
axi hygiene artifacts --repo owner/name --dry-run
axi hygiene artifacts --repo owner/name --enforce
axi hygiene stat storage --repo owner/name --json
```
