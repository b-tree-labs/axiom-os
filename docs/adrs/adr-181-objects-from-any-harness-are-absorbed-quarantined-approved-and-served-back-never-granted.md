# ADR-181: Objects from any harness are absorbed, quarantined, approved and served back; an import is never a grant

**Status:** Proposed (2026-10-08)
**Related:** [ADR-087](adr-087-cross-mem-portable-cross-harness-memory.md) (the four verbs and `SourceOrigin`, reused here), [ADR-088](adr-088-memory-recall-projection-corpus.md), [ADR-092](adr-092-cross-mem-continuous-bidirectional-sync.md) (echo suppression, write-back cadence), [ADR-063](adr-063-skill-md-as-generated-artifact.md) and [ADR-117](adr-117-skill-conformance-steward.md) (SKILL.md in both directions, generated versus authored), [ADR-072](adr-072-capability-projection.md) (one capability, many surfaces), [ADR-114](adr-114-mcp-authority-enforcement.md) (identity and effect gates), [ADR-152](adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md) (classifiers advise, labels decide), [ADR-175](adr-175-export-control-is-enforced-at-the-code-not-read-from-result-text.md), [ADR-032](adr-032-standards-positioning-dual-track.md), [spec-cross-harness-portability](../specs/spec-cross-harness-portability.md) (the formats, the record, the sweep, the journeys)

## Decisions Ben must make

Each has a recommended default; the rest of this ADR assumes the defaults.

| # | Decision | Recommended default |
|---|---|---|
| 1 | Is "nothing imported is visible to an agent until a human approves it" the standing posture, with no trusted-source exemption? | Yes. No exemption by harness or by path. |
| 2 | Allow a bulk approval for a user's own scope? | Yes, explicit flag, only objects whose scan is clean, one approval record per object. Anything under a plugin or marketplace cache is third party and excluded from bulk. |
| 3 | Where do imported objects live? | In the memory ledger as `procedural` fragments (`resource` for MCP registrations), trust as separate append-only records. Not a new store. |
| 4 | CLI noun | `axi harness` (purpose-named, ADR-056). |
| 5 | Where may Axiom write on emit? | Only locations marked `verified-by-run` in the harness descriptor, only files it owns by path prefix `axi-`. First skill target: `~/.claude/skills/axi-<name>/` (OpenCode reads that path, verified; Claude Code's use of it is documentation-level until checked). |
| 6 | May a foreign agent definition run inside Axiom's own chat? | Not on import. A second, separate approval ("enable in chat") with tool mapping shown. |
| 7 | Name collision with a registered command, built-in skill or tool | Imported object takes `name@<harness>`; it never shadows. |
| 8 | Hooks and bundle code | Never imported, translated or emitted. Inventoried and reported only. |
| 9 | Sync foreign deny rules (a narrowing, so safe) | Later phase, default off. Foreign allow rules are never imported. |
| 10 | Phase order | Inventory, then skills, then serve (skills and commands), then agent definitions; sessions run as their own track. |

## Context

Ben's statement: cross-harness portability is a core feature and a core value.
Cross-mem already delivers it for memory: absorb, migrate, sync and serve, with a
write-once origin coordinate, entity-resolution dedup, a fail-closed serving
gate and write-back to the instruction-file layer (ADR-087, ADR-092; the absorb
adapters under `src/axiom/memory/absorb/`).

Users carry more than memory between harnesses. A survey of the harnesses on the
reference machine (spec section 1) shows:

- Skills have converged on one file format (`SKILL.md` folders) and two shared
  folders. OpenCode auto-loads `~/.claude/skills` and `~/.agents/skills`
  (`verified-by-run`). Copying files is solved by the harnesses themselves, and
  badly: no inventory, no collision handling, no review, no classification.
- Agent definitions, user commands and prompts exist in every harness with
  different locations and fields; hooks differ most. Sessions are JSONL or SQLite
  per harness; a `/renew` extension is being built to aggregate them
  (`feat/chat-renew`).
- Axiom already reads a SKILL.md back into a capability that returns the
  procedure, parses `allowed-tools`, validates names as identifiers and refuses
  path-like names (`src/axiom/infra/skill_md.py`), and already mirrors the
  user-command convention (`chat/user_commands.py`). ADR-117 already names
  documents "imported from another harness" as authored, validated and never
  regenerated.
- The chat command set is declared once in a registry (ADR-168, on
  `feat/chat-commands-and-placement`). Most commands are chat-only, a few manage
  portable objects, several are cross-harness views, and several are already
  reachable through MCP tools (spec section 3 classifies every one).

The new risk memory did not carry: a skill, agent definition or command is
instruction content. It cannot grant authority by itself, but it steers a model
that holds authority, and a third party's text will reach that model. Importing
by copying inherits every harness's trust assumptions at once and gives the user
no place to see them.

## Decision

**D1. Scope.** Portable object kinds are: skill, agent definition, user command
(prompt), MCP server registration, session. Memory and rules files stay with
cross-mem. Hooks and plugin bundle code are excluded (D8). A bundle is unpacked
into kinds and keeps its marketplace and commit as provenance.

**D2. Reuse the four verbs and the origin coordinate; add nothing parallel.**
Absorb adapters gain a second optional capability, `scan_objects(kinds)`, beside
`scan()` for fragments and `SessionCapable` for sessions. It is read-only,
makes no network call, executes nothing, and parses YAML with safe load. Every
object is stamped with `SourceOrigin`; `(harness, account, source_ref)` is the
idempotency key with `source_ref = <scope>:<kind>:<relative path>`. Migrate is
the same import primitive between accounts. Sync is the same primitive applied
continuously, producing `changed` versions (D4), never auto-approval. Serve is D6.

**D3. One canonical form, stored in the ledger.** The portable object record
(spec 2.1) is a normalized, SKILL.md-superset: description and body kept
verbatim apart from encoding and whitespace normalization, the source's tool
list kept verbatim next to its mapped form, unknown frontmatter kept as `extras`
so a return to the origin harness restores it. It is written through
`CompositionService` as a `procedural` fragment, inheriting immutable
provenance, dedup, the serving gate, bundle export and audit. Callable and
emitted forms (a registered `SkillSpec`, an emitted file) are disposable
projections rebuilt from approved records. Objects are excluded from the recall
corpus until approved, and then reach agents through the library door, not memory
recall.

**D4. Quarantine by default; approval is a human act, pinned to a hash.** A
discovered object is inventoried (name, hash, origin, scan report) and is
invisible to every agent, unregistered on every surface, unemitted and unrecalled.
Approval is an append-only record by a human principal (ADR-035) naming the
content hash, classification, exposure set (surfaces and harnesses) and the
review summary shown. Any change in the source content produces a new version
that is `discovered`; the last approved version keeps serving until the human
decides. Revocation removes emitted copies at the next session boundary. The
approve verb admits only human principals through the ADR-114 identity gate and a
headless surface refuses it (the non-interactive approval policy), so no agent,
including one operating on a skill's instructions, can approve its own input.

**D5. Never widen.** A foreign tool grant is a request. The effective set is the
mapped request intersect the invoking principal's grants intersect the surface
policy and site rules. Mapping targets capability classes (`read`, `write`,
`exec`, `network_read`, `delegate`); shell prefix patterns are not translated
into pattern allows because their matching semantics belong to the harness;
unknown names are dropped and reported; deny entries carry over; allow-all
wildcards are never imported. `delegate` is not granted at all initially.
Model, temperature, mode and similar hints are recorded and never applied
(model choice belongs to tier policy, ADR-054). Every conversion produces a
report with `fidelity` (`exact`, `structural`, `lossy`), `dropped`, `approximated`
and a `widened` list that must be empty, enforced by test.

**D6. Classification, secrets, supply chain.**
- *Unknown classification is deny, not public.* An imported object may always be
  served back to its origin coordinate (it already lives in that store); serving to
  any other harness, account or deployment tier needs a label set at approval whose
  policy admits that coordinate, enforced by the existing single serving gate.
  Emission honors each harness's `model_routable` flag; a harness whose model path
  is unverified is treated as outside the controlled tier. Export control remains
  enforced at the controlled code and at tool calls (ADR-175), not by reading
  object text; the allowed-tools mapping is what keeps an imported skill from
  reaching a controlled tool the caller could not already reach.
- *Secrets.* The cross-mem `looks_like_secret` classifier runs on every object;
  a hit is `blocked` (it is not routed to the vault, because an instruction is not
  a credential), listed by pattern name only, and cleared only by a new content
  hash. Reports and logs never print the value.
- *Scan advises, a human decides* (ADR-152): zero-width and bidirectional control
  characters, injection phrases, fetch-then-execute shapes, base64 blobs, and
  bundled executables are surfaced in the review summary. Bundled scripts and
  assets are listed by name and hash, never imported as runnable.
- *Supply chain.* Provenance records harness, account, path, bundle, marketplace
  and commit. Third party versus own scope is derived from the path. Absorb
  never fetches over the network (a URL-referenced skill list is not followed),
  never executes, validates names as identifiers, refuses symlinks and path-like
  names, and caps size and count. Approval is by content hash; there is no
  auto-update from a source. Objects stay local to the principal; promotion to
  an organization or federation is a separate ceremony with fresh review
  (ADR-027).

**D7. Serve through three doors, behind the one gate, writing only what Axiom
owns.** Native files: `harness.emit` writes `axi-<name>` entries to locations
verified for that harness, never touching a file it does not own, at session
boundaries (the ADR-087 D6 cadence rule); echo is suppressed with a content-hash
index (ADR-092 D1 method) so emitted copies are never re-absorbed. MCP: the
existing capability projection carries `skills.get` and prompts, with the
prompts-as-tools door for clients that never call `prompts/list`. Index block:
a second managed block in AGENTS.md lists approved names and how to fetch bodies
(names in the cached prefix, bodies on demand). Emission refuses a name the
target lists as a built-in. After emitting, verification uses the harness when it
can answer (for OpenCode, `opencode debug skill` lists what it loaded).

**D8. One harness descriptor.** The `TOOL_SPECS` matrix
(`mcp/install.py`: path, format, detection, export-control posture) becomes the
single per-harness registry of where each object kind lives, the harness's
built-in names, and a basis mark (`verified-by-run`, `from-docs`, `unverified`)
per fact. Emission and absorb read it; nothing else hardcodes a harness path.
Hooks and bundle code are inventory-only (D1).

**D9. Names, dedup, precedence.** Identity is `(kind, normalized name)`; version
identity is the content hash. The three ADR-087 D3 tiers apply: exact collapses
with several origins; near-duplicate (frontmatter order, whitespace, harness
extras) merges reversibly as variants; conflict keeps both as `name@<harness>`,
flagged, never auto-merged. Registered commands, built-in and extension skills
and MCP tool names always outrank imports; project scope outranks user scope
among imports. Axiom never changes a harness's own precedence inside that
harness.

**D10. The registry declares exposure.** `Command` gains `expose` (subset of
`chat`, `mcp`, `harness-command-file`; default `chat`), `template` (a prompt body
shared by the chat handler and the emitted file), `capability` (for commands that
are a call, rendered into a "call this tool" stub via `capability_projection`),
and `cli` (the `axi` verb whose parameter schema the slash form shares, which is
the flag parity). A command with only a Python handler cannot be emitted because a
command file is a prompt; registry construction rejects `harness-command-file`
without `template` or `capability`. The sweep (spec section 3) sets the initial
values: `/review` first, then `/memory`, `/save`, `/signal`, `/alerts`.

**D11. Phasing** (spec 7): 0 inventory with the descriptor; 1 skill absorb,
quarantine, approval and registration; 2 serve skills and commands with the
`expose` field; 3 agent definitions; sessions as an independent track; 4
continuous sync and cross-harness views. Each phase is deliverable alone.

**D12. Standards posture (ADR-032).** SKILL.md is the interchange format and the
skill form stays valid under it; AGENTS.md hosts the index block; MCP prompts and
tools are the bridge for commands. Nothing emitted names AEOS, and the record is
described by behavior in terms of the public formats. The trust model is the
differentiator and is described as behavior, not proposed as a standard.

## Options considered

- **Copy files between harness folders (a sync tool).** Lost: harnesses already
  do this among themselves (OpenCode reads Claude's folder), and a copier would
  spread unreviewed third-party instructions to every harness at once with no
  record of who approved what.
- **Trust the user's own folders and auto-activate.** Lost: a user's skill folder
  mixes their writing with marketplace content, and a path cannot tell them apart.
  Auto-activation turns every plugin update into an instruction change nobody saw.
- **Translate foreign permissions into Axiom permissions.** Lost: matching
  semantics differ per harness, so a faithful translation is not available, and
  any approximation errs toward widening. Narrowing-only mapping loses some
  convenience and never loses safety.
- **A dedicated object store.** Lost: it would re-implement provenance, dedup,
  the gate, bundles and audit that the ledger already has. Kept as an open exit if
  recall pollution proves unmanageable.
- **Import hooks too.** Lost: hooks run commands with the user's authority on
  lifecycle events whose vocabularies differ per harness; neither safe to review at
  scale nor faithful to translate.

## Consequences

**Easier.** One inventory across harnesses, with collisions and third-party
content visible for the first time. Skills and commands written once reach every
harness that can be verified. Memory, skills, commands and sessions share one
origin coordinate, one dedup method, one gate and one audit trail, so a security
review has a single choke point per direction.

**Harder.** Approval is a human step per object (bulk helps for own scope only).
Conversion is lossy for agent definitions and some command fields; the report
makes that visible and does not remove it. Every new harness fact needs a
`verified-by-run` check before an emit target is enabled, and facts from
documentation will drift.

**Committed to.** Extending `TOOL_SPECS` into the descriptor; an object scan
capability on adapters; the `harness` skill functions; registry fields in D10 and
a generator in the chat extension; a fixed-point round-trip test per kind (the
ADR-117 method); a negative test that `widened` is always empty; a test that a
quarantined object is absent from every surface (chat tools, MCP, emitted files,
recall).

**Follow-up work.** Phase 0 descriptor and `axi harness scan`; read
`test/mcp-harness-collisions` when it lands and reconcile emitted names; add
Cursor and OpenCode session readers to the sessions track; write the user-facing
documentation for the review screen; decide the open items in the table above.

**Could not be determined here.** Codex's binary did not run (its launcher points
at a missing application), so its formats are from documentation or file
listings only. Gemini CLI, `cursor-agent` and Goose are not installed, so every
fact about them is `unverified`. The collision branch `test/mcp-harness-collisions`
has no commits yet. Claude Code's agent and command file locations are
`from-docs` (none exist on the reference machine). Whether Claude Code loads a
user-created skill under `~/.claude/skills/axi-<name>/` is documentation-level
until a run confirms it.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
