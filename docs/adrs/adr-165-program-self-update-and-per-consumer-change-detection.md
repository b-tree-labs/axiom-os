# ADR-165: Program tracking self-updates, and reports change per consumer

**Status:** Accepted (2026-10-06)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the composed CLERK), [ADR-162](adr-162-the-watcher-primitive.md) (the watcher primitive), [ADR-073](adr-073-mcp-registry-driven-tool-surface.md) (registry-driven MCP), [ADR-114](adr-114-mcp-authority-enforcement.md) (identity gate), [ADR-157](adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (serving budgets), `docs/prds/prd-program.md` (R3, R4, R11, R12), `docs/specs/spec-program.md`

## Context

Program tracking (ADR-161) shipped the read side: a data file per program,
the `status`/`validate` reads, the renderer, and the data-file `drift` scope.
Two requirements were unmet. The tracker must **self-update continuously**
rather than be assembled by hand (prd R3/R4), and a principal must be able to
ask **"what changed since the last time the program reported to me"** (prd
R12) — not just "what is the state now".

Three forces shape the answer. The data file is edited by hand, by the clerk,
and (later) by capture feeders, so detection must be idempotent and must not
assume a single writer (prd R11). "What changed for me" is inherently
per-consumer: two people who last looked at different times must get different
answers. And the change read has to be reachable from an MCP client and an
HTTP caller, where a tool that silently writes is a hazard — yet a watermark
that can never advance is useless.

The capture feeders themselves (GitLab/GitHub/wiki/transcript, the ADR-162
watcher instances) are a later phase. This decision is the detection and
schedule core they will write into, and it has to stand and be testable
before any network capture exists.

## Decision

**The data file is the current state; an append-only change log is the
history beside it.** `<state_dir>/program/` holds `data.json` (current),
`changelog.jsonl` (append-only history), and `snapshot.json` (the diff
memory). Every write goes through `axiom.infra.state` (`locked_append_jsonl`,
`LockedJsonFile`), never a bare `open()`.

**`sync` is the idempotent reconcile.** It reads the desired state from a
pluggable, read-only **source** (a `ProgramSource`: an `origin` label plus
`load()`), diffs it against the last snapshot, appends one change-log entry
per difference, and advances the snapshot. The snapshot is a content hash per
item and per field, so a change seen twice is not logged twice — the drift
resilience. The whole reconcile runs under one exclusive lock on the
snapshot, so concurrent syncs serialize rather than double-log. Phase 3 ships
`FileSource` and `NullSource`; a future feeder implements the same seam.
`sync` reads only read-only sources and writes only the node's own state, so
it is safe to run automatically.

**Change detection is per-consumer via a watermark.** Each principal has a
position in the log (`watermarks.json`); `changes` returns the entries after
it, in the same owner/dates/status/percent/links shape as `status`. A
watermark never moves backward, and the store is per-principal.

**Reporting is a read; advancing is a write; the two are separated.**
`changes` declares `side_effects=false`, so it projects as a read-only MCP
tool and an HTTP `GET`. Advancing is the default only on the operator's own
`cli` surface; on every served surface the default is peek, and advancing
requires an explicit `advance` the caller sets. The HTTP `GET` never forwards
`advance`, so it is unconditionally a read. The watermark write is the
caller's own local reading position — never program state, never anything
another consumer observes — which is why read-only is honest.

**The coordinator is the existing daemon-agent mechanism, not a new
scheduler.** CLERK (ADR-161) is declared as a `kind="agent"` provide-block
with an `[agent]` lifecycle block whose `heartbeat_command = "program sync"`;
the existing agent service runner fires `axi program sync` on the heartbeat.
Declaring it arms nothing: the master `autonomy.enabled` setting ships off,
and `default_consent = "opt-in"` keeps CLERK out of the auto-enabled core
set, so firing stays an operator decision.

## Options considered

- **Mutate the data file in place, no history.** Loses "what changed" and
  makes a hand edit indistinguishable from a feeder push. Rejected: the log
  is the feature.
- **A global "last reported" cursor, not per-consumer.** Cannot answer "since
  *I* last looked" for more than one consumer. Rejected against R12.
- **Advance the watermark on every read, everywhere.** Makes the MCP/HTTP
  tool a silent writer — a GET with a side effect. Rejected: a read tool must
  stay a read; advancing is opt-in off the CLI.
- **Never advance on a served surface.** Honest, but makes the watermark
  unusable to the agent the feature is for. Rejected in favour of explicit
  opt-in.
- **A new program-specific scheduler.** Re-implements the daemon substrate
  ADR-161 already composes. Rejected: declare an agent, reuse the runner.

## Consequences

- Re-running `sync` converges rather than duplicating; a missed webhook, a
  hand edit, or a second writer is healed on the next cycle, and the log says
  what changed and from which source.
- A consumer's first `changes` call on a fresh log returns the whole current
  program as added/opened — correct for a watermark that starts at zero.
- The change-kind vocabulary is closed and stable, so a renderer or a filter
  keys on `kind` without parsing prose; extending it is a deliberate edit.
- The source seam is the single integration point for the capture phase: a
  feeder becomes another `ProgramSource`, and nothing downstream changes.
- The read/advance split is now a contract other serving tools can copy: a
  capability whose default is a read but which can perform a caller-requested,
  caller-local write projects read-only and gates the write behind an
  explicit flag off the CLI.
