# ADR-248: One writer per program; bound installs read

**Status:** Accepted (2026-11-10, decided by the program owner)
**Amends:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (program tracking is a composed CLERK)
**Related:** [ADR-195](adr-195-a-node-has-a-binding-and-a-sandbox.md) (a node has a binding and a sandbox), [ADR-242](adr-242-clerk-writes-progress-from-cited-evidence-never-dates.md) (progress from cited evidence), [spec-program](../specs/spec-program.md) §One writer per program

## Context

A program's data file, change log and snapshot are written by `program sync`,
and its status page by `program publish`. Nothing said which install does
that. A program is typically served from one node, while people also run
CLERK on laptops bound to that node (ADR-195) to read its status. When the
laptop ran `sync`, it wrote its own copy of the data file, appended its own
change log, advanced its own snapshot and, with a wiki declared, could
publish. Two installs then held two diverging records of one program, each
with its own idea of what had already been logged, and either could
overwrite the public page with an older view. The live feeders also advanced
the laptop's watcher cursors, so a laptop later made the writer would skip
everything it read but never wrote.

## Decision

A program has **one writer**: the install named by `program.writer` (its
node id, the name the topology already uses: `AXIOM_NODE_NAME`, else the
short host name). Only that install writes the program's data file, change
log, snapshot and freshness stamp, and only it publishes.

Every other install that runs `sync` or `publish` is **read-only**. It reads
its sources as usual, reports what it would change (`would_change`), says
it is read-only in its result (`read_only: {writer, this_install, detail}`),
and writes nothing: not the program files, not its watcher cursors, not a
wiki. A dry-run publish still renders locally, since it writes no program
state and publishes nothing.

When `program.writer` is absent, every install writes, exactly as before.
The key is opt-in so no existing deployment changes behaviour.

## Options considered

- **Last writer wins.** Simple, and wrong: the change log is append-only
  and per install, so two writers double-log or miss changes, and an older
  view can overwrite the published page.
- **Writes from any install, merged on the writer.** Needs a merge protocol
  for the data file and the log; the person-facing value is nil, because a
  bound laptop already reads the writer's answers through its binding.
- **Infer the writer from the binding** (a bound install never writes).
  Close, but a node can hold a binding for other reasons, and an unbound
  laptop can still be a second writer. Naming the writer says it once, in
  the program's own file.

## Consequences

- Running CLERK on a laptop is safe: it can show what a sync would change
  without forking the program's record.
- Changing the writer is a one-line edit of `program.writer`; the new writer
  has not advanced cursors it never wrote, so its first sync reads in full.
- The person-facing mutation verbs (`program item edit`, `program person add`, …) are not gated by this
  decision; they edit the file a person points them at. Gating them is a
  separate call if bound installs start editing local copies.
