# ci_sweep

The hourly CI-health check on the schedule. It reads the same things as
`ci_health`, then:

1. Cancels superseded runs: an older run on a branch when a newer head is
   running, and an older default-branch run when a newer one tests a
   descendant. Never a release, publish, tag or deploy workflow, a tag ref,
   a scheduled or manual run, or the newest run. At most `max_cancels` per
   sweep. Each cancel writes an audit record under
   `agents/rivet/ci-health-records`.
2. Records the budget state in `agents/rivet/ci-health.json`. `land --act`
   obeys it: at `warn` landing waits for a batch and refuses blind re-runs;
   at `stop` only release pull requests land.
3. Alerts through notifications when the budget crosses warn or stop, the
   run-rate passes `run_rate_multiple`, or a waste signal passes
   `waste_share`. Each alert names the one action RIVET took and the one
   decision left to a person. An alert is sent once per dedup key.

`--dry-run` proposes the cancels and sends nothing.

`ci_digest` sends the weekly digest: the same reading, worst signals first.
