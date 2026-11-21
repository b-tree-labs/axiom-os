# ADR-154: A lane reserves the next number in a document series

**Status:** Proposed (2026-10-02)
**Builds on:** [ADR-141](adr-141-lane-is-shared-by-two-agents.md) (the lane is deterministic; its agents only propose), [ADR-046](adr-046-rivet-tidy-boundary.md) (RIVET signals, TIDY reclaims), [ADR-052](adr-052-database-tenancy-schema-per-extension.md) (schema-per-extension; lanes isolate by database)

## Context

Several document kinds in this portfolio are a **monotonic series**: ADRs
(`adr-NNN-…`), and anything else numbered by convention. The next entry's
identity is "one past the current maximum," which every author computes the
same way and at the same moment.

When one person works alone the computation is correct. When several agents
work in parallel worktrees — the normal case now — it is a race. Two agents
each read the maximum, each add one, and each write the same number onto a
different branch. Neither is wrong by its own evidence; the collision only
exists in the union, which no single checkout can see. ADR numbers have
collided here repeatedly for exactly this reason.

`scripts/lint_adr_numbers.py --next` was taught to look past one checkout: it
scans git refs and, more recently, sibling worktrees on the machine. That
closed the worst blind spots, and two standing notes record why it had them —
a number looked free because `--next` could not see sibling branches, and an
ADR number is free only if absent from **both** refs and disk, because
untracked files on a sibling worktree's disk are invisible to `git log`. But
`--next` is a **read with no reservation**. Two agents that run it a second
apart still get the same answer. The missing fourth input is an outstanding
reservation: a number is free only if absent from refs, from disk (every
worktree), from sibling branches, **and** from a record that says "somebody is
about to use this one."

There is already a component whose entire job is deconflicting contended
resources across parallel checkouts on one machine: `axi lane`. It hands out
port pairs and databases from a shared registry it mutates under a lock, so
two worktrees never collide on a socket or a schema. A document-series number
is the same shape of resource — a scarce, ordered, machine-global allocation —
and the registry that already serializes port and database claims is the one
place a reservation can be recorded where every worktree will see it.

## Decision

`axi lane` gains a **document-series reservation** primitive. It is the same
kind of allocation it already does for ports, recorded in the same registry,
under the same lock.

1. **A series is declared, not assumed.** A series definition is repo
   configuration: an id (`adr`), a filename shape (prefix `adr-`, three-digit
   zero-padded number), and one or more location globs the series lives under.
   A kind that lives in more than one place declares more than one glob (an
   extension-local decision series under ADR-031 is the standing example).
   The platform defines the *shape* of a series definition; a repo fills it in.
   This keeps the primitive domain-agnostic: `axi lane` never knows what an
   "adr" is, only that it is a padded-integer series under some globs.

2. **`axi lane reserve <series>` allocates atomically.** Under the registry
   lock it computes the next free number — free meaning absent from refs, from
   disk across all worktrees, from sibling branches, and from every outstanding
   reservation — writes a reservation into the registry, and returns the number
   and the conventional filename stub. Two concurrent calls serialize on the
   lock and receive different numbers, which is the whole point.

3. **A reservation is held like a port, and retired the same two ways.** It
   records the lane, branch and owner. It is retired when the document lands —
   RIVET already signals a branch's merge, and once the numbered file is in
   trunk the reservation has served its purpose and drops — or it is reclaimed
   when the branch is abandoned, which TIDY proposes and never executes, reusing
   the reclaim/signal split from ADR-046 and ADR-141 unchanged. A reserved
   number whose branch never landed leaves a **hole**, and a hole is acceptable:
   an ADR is immutable and never renumbered, so a skipped number costs nothing.
   A collision costs a rename across a signed history or a merge conflict in an
   immutable record, so the asymmetry runs entirely one way.

4. **One source of truth, not two.** The free-number computation is the logic
   in `lint_adr_numbers.py`. That script becomes a consumer of the lane ledger —
   it asks the registry rather than scanning independently — so the reservation
   and the validation agree by construction instead of racing.

5. **Deterministic at the core (ADR-141).** `reserve` is a pure allocation
   under a lock, exactly like `allocate_ports`. No agent is required to reserve,
   and `reserve` consults no model. The agent halves only *observe*: RIVET
   retires a landed reservation, TIDY proposes reclaiming an abandoned one. The
   reservation itself is as agent-free as a port claim.

## Consequences

- **Migration revisions are the same race, and are deliberately out of scope
  here.** An Alembic revision chain collides the same way two worktrees each
  generate a revision, but a revision is not just a padded integer — it carries
  a `down_revision` link, so deconfliction has to reason about ordering, not
  only uniqueness. That is a follow-on that reuses the reservation ledger with
  a richer series definition; this ADR scopes the primitive to numbered-filename
  series first, where "next integer" is the whole of the allocation.

- **Series are per-repo and independent.** One repo's `adr` series has nothing
  to do with another's. The reservation key is `(repo, series)`, matching how
  the ADR series is already per-repo (one `NNN` run per repo).

- **Reservation is a developer-machine act; validation is the cloud half.** A
  reservation lives in `~/.axi/local/`, which a cloud CI runner cannot see, so
  CI cannot reserve. CI can still *validate* — fail a pull request whose series
  number already exists in trunk — which is the collision check that does not
  need local state. The reservation prevents the collision locally; the CI
  check is the backstop for a number that was taken without reserving.

- **`reserve` is a write, and surfaces as one.** It mutates the shared registry,
  so it follows the same surface posture as `lane claim` / `lane hold`: a
  side-effecting verb, gated, offered on CLI and agent-tool, declaring its
  effect. `release`/reclaim of a reservation follow the existing lane lifecycle.

- **What this does not change.** Port and database allocation, the registry
  format's existing fields, and the reclaim/signal agents keep their behaviour;
  this adds a resource kind to a mechanism that already exists, rather than a
  new mechanism.

## Not yet built

This ADR is **Proposed**: it records the decision and enough design to build
from. No `reserve` verb, series-definition schema, or `lint_adr_numbers`
rewrite has shipped. The reclaim/signal halves it leans on are in place
(`axi hygiene reclaim`, RIVET's branch-landed signal). Accepting this ADR, or
revising it, precedes implementation.
