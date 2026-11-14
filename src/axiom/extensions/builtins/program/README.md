# program

Program tracking: one data file per program (`axiom.program/0.1`), kept current by
CLERK. Requirements are in `docs/prds/prd-program.md`; the design is in
`docs/specs/spec-program.md`.

## Turning it on

Program tracking is an optional feature: installed with the platform, off on a
fresh node.

```bash
axi features enable program     # on: commands, MCP tools, routes, CLERK, schedules
axi features disable program    # off again (recorded, so it stays off)
axi features list               # shows it as an "extension" row, on or off
```

A node that already has program state (`<state_dir>/program/`, for example a
`data.json`) counts as on without running anything, so upgrading never switches a
live tracker off. `disable` wins over that.

## Runbook: the scheduled sync

`program.sync`, the weekly consolidation pass and the follow-up sweep run on the
platform scheduler (PULSE). They are declared in this extension's manifest
(`[[extension.schedule]]` with `hosted = true`), and the scheduler host registers
them when it starts, without naming this extension. Nothing needs to be installed
on the node.

Configure it in the data file, under `program`:

```json
"cadence": {
  "sync_minutes": 15,
  "enabled": true,
  "consolidate": "0 8 * * 1",
  "consolidate_enabled": true
}
```

Every key is optional; the values shown are the defaults. The scheduled sync
retunes its own rows to these after each run, so a changed interval takes effect
from the next run and `enabled: false` pauses the row.

What a scheduled run does:

- It takes a non-blocking lock on `<state_dir>/program/sync.lock`. If another sync
  holds it, the run returns `skipped_run: overlap` and does nothing.
- On a node with no program data file it returns `skipped_run: no_program` and
  records nothing.
- Over an unchanged source it logs nothing.
- If it changed something and the program sets `program.publish.after_sync: true`
  with a `program.publish.wiki` target, it then runs the publisher
  (`program.publish`, or the skill named in `program.publish.after_sync_skill`).
  A publish failure does not fail the sync; it shows as `freshness.publish` in
  `program status`. Publishing after sync is off by default.

Check it:

```bash
axi schedule list                      # rows for program.sync and program.consolidate
axi program status --scope freshness   # last verified sync, last publish outcome
```

### Retiring a node-local sync timer

A node that ran `program sync` from its own timer (a systemd timer, cron) can drop
it once the orchestrator service runs there.

1. Confirm the orchestrator is running and the row exists:
   `axi schedule list` shows `program.sync` active.
2. While both exist, make the timer share the lock so the two can never overlap:
   wrap its command in `flock -n <state_dir>/program/sync.lock axi program sync`.
   Whichever starts second skips.
3. Watch one interval: `axi program status --scope freshness` advances
   `last_attempt_at` with the timer stopped (`systemctl stop <timer>`).
4. Disable and remove the timer (`systemctl disable --now <timer>`, then delete its
   unit files). Rollback is re-enabling it; the lock keeps that safe.

The opt-in CLERK `[agent]` heartbeat (`heartbeat_command = "program sync"`) can stay
as it is: when an operator enables it, it shares the same lock.

## Runbook: the consolidation digest

`axi program consolidate` runs one pass and writes the digest to
`<state_dir>/program/digests/latest.json`; `axi program digest [--kind <kind>]` reads
it (also an MCP tool). The weekly scheduled pass also delivers it once through
HERALD to `AXI_FOLLOWUP_OPERATOR`, else the program deputy. It never re-sends an
unchanged digest, never sends an empty one, and never sends from an install that is
not `program.writer`.

Every entry is a proposal with its evidence. Nothing is applied: entries whose change
belongs in the data file carry the `apply` command (a `program item ...` verb); the
rest are changes to make on the tracker.

To attribute repository work to lanes and epics, declare `program.repo_map`:

```json
"repo_map": [
  {"repo": "example-org/api-*", "lane": "platform", "epic": "e-api"},
  {"repo": "example-org/web", "lane": "apps"}
]
```

Other settings: `program.entropy.stale_days` (default 30) and `program.taxonomy`
(`lane_prefix` `lane:`, `program_prefix` `program:`, `epic_label` `epic`,
`placeholder_titles`).
