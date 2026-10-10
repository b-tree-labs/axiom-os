# reclaim

TIDY's lane-reclaim surface. Lists the development lanes that could be given
back — a lane whose checkout is gone, or whose branch has landed — and the
commands that would do it. It **proposes and never acts** (ADR-141): the drop
is a command you run through `axi lane`, deliberately, not something an agent
does on your behalf.

## When to use

- You are deciding what parallel-development state is safe to clean up.
- A sweep wants the stale-lane list without touching anything.

## What it does

Reads the lane registry, then for each lane asks two questions it is allowed to
ask (is the checkout still on disk? is any venv still importing from it?) and
one it is not — whether the branch has landed. That last answer is RIVET's
(`release.lane_signal`), injected; when RIVET is unavailable every branch reads
as *cannot tell*, which blocks, because unsure must never reclaim.

A lane that is bound — a running server's virtualenv still imports it — is
reported as **blocked** however finished it looks by git. That is the F2 floor,
and it is the whole reason this is a separate call from the drop.

## Output

- `reclaimable` — lanes with a proposal and no block. Each carries the ordered
  commands (`axi lane release <name>`, and for an isolated database a printed
  `dropdb` marked irreversible).
- `blocked` — lanes something still depends on, or that could not be assessed,
  with the reason.

## Surfaces

`axi hygiene reclaim` (add `--json` for the structured payload); the same skill
is projected to MCP and agent-tool as a read. It writes nothing on any of them.
