# ADR-162: One watcher primitive feeds the bus; sources get exactly one reader

**Status:** Accepted (2026-10-05)
**Related:** [ADR-060](adr-060-event-routing.md), [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md), the release extension's watchers (`trunk_health.py`, `pr_check_watcher`, `routine_monitor.py`, `local_sync.py`)

## Context

The poll-and-emit shape exists in at least four bespoke copies in the release
extension alone; `trunk_health.py`'s own docstring names the shared shape. The
program extension (ADR-161) needed feeders for GitLab, GitHub, wikis, and
meeting transcripts, which would have been copies five through eight. Each
bespoke copy re-implements cursor state, debounce, and emission, and each
invents its own output, so consumers couple to capture details. Mirrored
repositories add a double-count hazard, and activity on non-default branches
was being silently missed (the 2026-10-05 repo sweep undercounted for exactly
this reason).

## Decision

Axiom ships ONE watcher primitive: a shared implementation of cursor state,
debounce, and capture by webhook or by cursor-based delta poll, publishing
normalized events onto the EventBus. Extensions declare INSTANCES only: a
source, a credential, and a subject mapping.

Three rules ride the primitive:

1. **One reader per external source.** Exactly one feeder publishes a given
   source; every other consumer subscribes to the bus and keeps no private
   copy.
2. **One event schema regardless of capture mode.** A consumer cannot tell a
   webhook from a poll, and never needs to. Cursors key on content identity
   (commit SHAs), so a mirror and its origin collapse to one event, and
   origin webhooks are the only code-evidence feed for mirrored repos.
3. **Branches are first-class.** Capture covers all refs and merge requests,
   never the default branch alone; events carry landed versus in-flight so
   consumers can report the two distinctly.

## Options considered

- **Keep per-extension watchers.** The status quo; every new consumer adds a
  copy and a divergence.
- **A sensing agent.** Sensing has no judgment to make and no consent to
  manage; making it a persona adds scheduling surface without adding
  accountability. Sensing is a library; agents consume events.
- **The shared primitive (chosen),** born with a real consumer: the program
  extension's first feeder is the primitive's first instance.

## Consequences

- The release extension's watchers migrate opportunistically, when next
  touched, never as a big-bang rewrite.
- The bus subject grammar for capture (`program.scm.push`,
  `program.tracker.issue`, `program.wiki.page`, and successors) is owned
  here; the journal timeline gains these lanes with no extra integration.
- A source that only proved "the API answered" is not verified: instances
  are expected to pass the connections ladder (reach, auth, per-project read,
  scratch write where writes are declared, webhook delivery onto the bus).
