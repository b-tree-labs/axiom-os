# ADR-198: PULSE is the only clock

**Status:** Accepted (2026-10-22)
**Related:** [ADR-196](adr-196-a-follow-up-is-a-tracked-commitment-clerk-owns-it-the-scheduler-keeps-the-clock-and-herald-delivers.md) (CLERK owns a follow-up, PULSE supplies the clock, no new daemon), `docs/prds/prd-axiom-schedule.md` §13, `docs/specs/spec-axiom-schedule.md`, axiom-os#274 (`axi schedule install`, the host-unit wake-up)

## Context

Looping work has been growing its own timers.

- PULSE (`extensions/builtins/schedule`) already registers a cadence
  (one-shot, interval, cron, rrule), fires it once, retries, and dead-letters.
  The PULSE agent is that clock. ADR-196 uses it for follow-ups and refuses
  to add a daemon.
- `axi schedule install` writes a host unit — cron, launchd, a systemd user
  timer, or a Windows task — from `deploy/<host>/`. That is how a machine
  wakes a process. It is not a list of jobs.
- A node chart (`jobs:`) turned each job into a Kubernetes CronJob, and on a
  single host into a `while true; do …; sleep` loop with its own list of
  accepted cron forms. The job command runs on that timer. PULSE is not told.

The host installer and the chart loop both answer "when does this command
run?" so it looks like reuse to call `axi schedule install` from the chart.
It is not. A chart job is a container with the node image and a role's
volume. A host crontab runs outside that container. Pointing the chart at
the installer splits the job from the node that holds its data.

Leaving the shell loop in the chart is the other mistake. It is a second
parser and a second clock, so retry, idempotency, misfire, and dead-letter
apply only to the jobs that happened to register with PULSE.

## Decision

**Every loop is a PULSE cadence. One process ticks. Nothing else keeps a timer.**

1. A job is `register(cadence, action)`. The action is a skill. Overlap,
   retry, misfire, and dead-letter are PULSE's. A node job that must run
   inside a container or on a volume says so on that record. If PULSE cannot
   say it yet, the schedule extension grows. The chart does not.
2. `axi schedule install` wakes the process that hosts PULSE. One entry per
   host. It does not name the partner-health check, the edge pull, or any
   other job. Kubernetes uses one wake-up for the same process, not one
   CronJob per job.
3. A `while true` sleep loop and a per-job CronJob are not schedulers. They
   are the shape this decision retires. A chart `jobs:` entry is the cadence
   in (1), not a CronJob whose args are the job command.

## Consequences

- Adding a scheduled job does not add a crontab line, a launchd plist, or a
  CronJob.
- The chart generator stops owning a cron grammar. The three forms the shell
  loop accepted (`*/N * * * *`, `M * * * *`, `M H * * *`) are not a second
  dialect. Cron in a cadence is the dialect PULSE already parses.
- Host install stays the way a machine that is not running PULSE yet is
  started. It does not become the way a node job is defined.
