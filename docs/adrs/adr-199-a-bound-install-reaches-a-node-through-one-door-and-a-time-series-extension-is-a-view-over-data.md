# ADR-199: A bound install reaches a node through one door, and a time-series extension is a view over data

**Status:** Proposed
**Date:** 2026-10-22
**Related:** [ADR-049](adr-049-data-platform-orchestration-boundary.md) (data-platform boundary), [ADR-128](adr-128-medallion-tier-boundaries.md) (tier boundaries), [ADR-131](adr-131-there-is-always-a-medallion.md) (there is always a medallion), [ADR-157](adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (serving budgets), [ADR-180](adr-180-a-producing-site-keeps-a-local-medallion-and-shares-from-it.md) (a producing site keeps a local medallion), [ADR-200](adr-200-a-value-has-one-class-measured-predicted-estimated-or-simulated-and-its-feed-is-shown-with-it.md) (value classes and feed handles), [audit](../working/data-architecture-audit-2026-10-22.md)

## Context

On 2026-10-22 a harness's MCP read this machine's own empty database while the
person believed it was reading a remote site's node. Chasing that found that one
question, "what does the node I'm bound to say?", is answered by **five
independent mechanisms**, and that the answer depends on which verb you used:

| Surface | How it reaches a bound node | Config | Credential | Store it reads |
|---|---|---|---|---|
| `data` gold verbs (`series`, `aggregate`, `compare`, `describe`, `roles`, `tables`) | `read_target.ask` to the face's `/data/query` (6 verbs) | `site-bindings.json` | `<label>-key` in the vault | `silver.signals` through gold views |
| `data` medallion verbs (`catalog`, `describe`, `freshness`, `sample`) | did not follow the binding at all; read the local database | none | none | local `silver.signals` |
| `data` operator verbs (`gaps`, `tier-audit`, `pipeline-proof`, ...) | local only | none | none | local |
| `telemetry` verbs (`metrics`, `latest`, `aggregate`, `series`) | its own `RemoteClient` to its own `/telemetry/*` router | `telemetry.toml` (formerly `triga_telemetry.toml`) | its own token | its own long-format table |
| Site-side features (collector, enroll, status, ~15 modules) | each reads the binding file itself | `site-bindings.json` | various | n/a |

The telemetry path is the clearest duplication. The consumer's telemetry
extension owns a second store (a long-format table of `site, unit id, unit type, source_class,
metric, ts, value, unit, quality`, which is `silver.signals` with different
column names), a second landing path
(`land.py` projects staged CSVs straight into it, bypassing bronze and conform), a
second set of gold queries over that table, a second router, a second client, a
second config file and a second credential path. It also holds the largest table
on the node. Every property the data platform earned later (tenant isolation by
credential, serving budgets, provenance, pipeline proof, the stamped schema, the
bound-install rules) has to be re-earned there or is silently absent.

The cost showed up as failures, not as untidiness:

- A person bound to a node got an empty answer from one verb and a refusal from
  another and had no way to tell why.
- A rename of the extension (2026-10-02) changed MCP tool names and the config
  filename; a harness that had stored the old names broke without a message.
- The credential in the telemetry config was bound to a different site than the
  data the person meant to read, so a "successful" call returned nothing.
- Three people reading a figure from two paths can get two answers, because the
  two stores are fed differently.

The binding itself is also in the wrong layer. The file is Axiom's
(`read_target` reads `site-bindings.json`), but it is written, parsed and
re-read by the consumer's site extension and by ~15 of its modules.

## Decision

### 1. One binding primitive, owned by Axiom

`axiom.infra.binding` is the only code that reads or writes the binding file,
resolves the face URL, and looks up the bound credential. `read_target` and the
consumer's site extension import it; no other module parses
`site-bindings.json`. The file format does not change, so a machine that bound
before this ADR keeps its binding.

### 2. One door: a read verb that can be served is served, by registry, not by a list

A skill declares `serve = "face"` when it is a read that a node can answer for a
caller. The face's `/data/query` dispatches any registered skill carrying that
declaration, under the same authz, tenant scope and serving budgets as a local
call. The hand-kept lists (`read_api._VERBS`, `medallion._REMOTE_VERBS`) go away.

The client side is one function. A bound install follows the binding for every
servable verb with no per-verb code, and **refuses by name** for a verb that is
not servable (it never falls back to the local database). `axi data where`
reports which is which.

### 3. A bound install answers the same question the same way

For every servable verb, the answer from a bound install and the answer from the
node itself are equal for the same caller. This is tested, not assumed: a
conformance matrix runs each servable verb against a node (a real database) and a
bound install (a second process, bound through a real face) loaded with the same
data, and compares results. The matrix is generated from the registry, so a verb
added later is held to it without anyone remembering to add a test.

### 4. A time-series extension is a view over data, not a store

An extension that serves time series (the consumer's telemetry extension is the
case in hand) owns **names and conveniences**, not storage, transport or
credentials:

- Its verbs delegate to the data verbs (`metrics` to `catalog`, `latest` and
  `series` to `series`, `aggregate` to `aggregate`) with a declared mapping from
  its vocabulary (`metric`, its unit id) to the platform's (`channel`, feed).
- Its landing goes through bronze and conform like every other source. No
  extension writes `silver.signals`-shaped rows to a table of its own.
- Its config and credential are the binding's. A key already issued under the
  extension's former name keeps working for one release.
- Its tool and verb names remain for one release as deprecated aliases that say
  so in their result, then are removed.

### 5. Silver is the warehouse; nothing reads it to a user

Silver holds the canonical rows from which projections and materialized views
are built. **A verb that answers a person or an agent reads gold** (projections,
rollups and materialized views over silver), or bronze only to tell an operator
what landed. No user-facing verb, on any surface, takes `tier = silver`.

- The generic `catalog`, `describe`, `freshness` and `sample` verbs accept
  `gold` (the default). A request for `silver` is refused, with a message naming
  the gold object or the operator verb that answers the question.
- Silver is read by named **operator** verbs whose job is the warehouse itself
  (`conform`, `pipeline-proof`, `tier-audit`, `gaps`, `rederive`), which are not
  servable to a bound caller and carry no `serve = "face"`.
- A gold object is a **projection**, not a pass-through. `gold.signals`,
  `gold.signals_uncertainty` and `gold.uncertainty_budget` are today plain
  `SELECT ... FROM silver.*` views, so reading gold is, in the common case,
  reading silver with a different name. They become projections the verbs are
  designed against (bucketed rollups, latest-per-channel, and materialized
  views or continuous aggregates where the serving budget of ADR-157 needs
  them); the pass-through views remain only as a compatibility surface until
  nothing reads them.

### 6. Every answer says which node answered

`provenance.served_by` (shipped in the change that precedes this ADR) is part of
the contract of every data answer, local, bound or kit.

## Phases

Each phase ships on its own and leaves the system better than it found it. A
phase is done when its **gate** passes, not when its code merges.

| Phase | Delivers | Gate |
|---|---|---|
| **P0** (done, #1365) | `served_by` on every data answer; `data.where`; a bound install refuses what the node does not serve instead of reading locally; kit results say they ran on the sandbox | tests: no database opened when bound; no credential in any block; two real databases show the install untouched by the kit |
| **P0b** Silver is not a user surface | the generic verbs refuse `tier = silver` with a pointer; the resolver for silver is removed from the user-facing registry; operator verbs keep their direct reads and are marked operator-only | a test over every registered read skill: none reads `silver.*` unless it is on the operator list, and the list is asserted to match the verbs that carry no `serve` declaration |
| **P1** Binding primitive | `axiom.infra.binding`; `read_target` and the consumer's site extension use it; the ~15 readers import it | a guard test fails if any other module parses `site-bindings.json`; an old binding file still resolves |
| **P2** One door | `serve = "face"` on skills; the face dispatches by registry; `catalog`/`describe`/`freshness`/`sample`/`gaps`/`pipeline-proof`/`tier-audit` servable where safe; hand lists deleted | the parity matrix (decision 3) green for every servable verb; an operator-only verb is refused by name |
| **P3** Telemetry delegates | the extension's verbs call the data verbs; `/telemetry/*` serves through `/data/query`; same credential accepted; aliases emit a deprecation notice | for a fixed dataset, each old verb returns the same points as its replacement; `pipeline-proof` unchanged |
| **P4** One store | landing goes through bronze and conform; the history in the extension's table is conformed into `silver.signals` (windowed, verified by reconcile, storage-checked before each window); reads stop touching the old table | `reconcile` agrees per channel per hour; `pipeline-proof` FLOWING for the migrated feeds; node storage after migration is not higher than before |
| **P5** One config | the extension's config file folds into the binding; the CLI verbs become `data` aliases | a node with only the legacy file still works and says what to change |
| **P6** Remove | the extension's router, client, gold and store code are deleted; the old table is archived and dropped by an explicit, approved step | nothing imports the removed modules; the archive restores a sampled window exactly |

Projections (the second half of decision 5) have no phase of their own here
because their shape follows the verbs: each servable verb that reads a
pass-through view is moved onto a projection as part of the phase that makes it
servable (P2), and the gate for that phase includes the verb's cost class
(ADR-157) holding on the full-size table. What remains after P2 is deleting the
pass-through views, which is part of P6.

P4's size is real: the extension's table is the largest on the node. Its
compression (ADR-189) is independent and still worthwhile now; P4 does not wait
for it and does not repeat it. Dropping the table is irreversible and is its own
step in P6, taken only on an explicit decision after a verified restore.

## Consequences

- A person bound to a node gets the same behaviour from every `data` verb, and a
  clear refusal where the node cannot serve one. The empty-local-database
  confusion cannot recur through a servable verb.
- The warehouse tier stops being reachable by accident: an agent that asks for
  silver is told where the answer lives, instead of being handed rows that no
  projection has shaped or bounded.
- The platform's serving budgets, tenant isolation, provenance and proof tools
  reach the telemetry verbs by construction.
- One credential and one config describe a binding.
- The consumer's extension shrinks to names, mappings and its own conveniences.
- Cost: P4 is a real data migration on a node whose disk is already tight, and
  it needs windows, verification and a restore test. The old verbs and names are
  carried for a release.
- Servable verbs become part of a public contract: adding `serve = "face"` to a
  skill is a decision about what a remote caller may do, reviewed like an API.

## Alternatives considered

- **Keep both and document the difference.** The difference is what hurt. Two
  stores answering one question is a correctness risk, not a documentation gap.
- **Make `axi data` call the telemetry extension.** Inverts the dependency:
  the platform would depend on a consumer's table.
- **Teach the telemetry router the new budgets and isolation.** Re-earns each
  property a second time and leaves the store, landing and credential
  duplicated.
- **Remove the telemetry verbs immediately.** Harness configurations and
  colleagues' scripts name them; a release of aliases with a notice costs little.

## Open questions

1. Which read verbs are safe to serve remotely? `gaps` and `tier-audit` read
   the database's shape and may stay operator-only. Decided per verb in P2 with
   the security note for each.
2. Where the history lives after P4 if storage cannot take a second copy: convert
   in place (the adoption machinery of ADR-189) versus migrate by window and
   drop. To be decided with a measured storage plan, not here.
3. How long the aliases live: one release is the proposal.
