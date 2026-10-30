# Cross-harness portability: skills, agents, commands and sessions

**Owner:** Ben Booth • **Status:** Draft • **Last updated:** 2026-10-08
**PRD:** [prd-cross-mem.md](../prds/prd-cross-mem.md) (the memory half of the same program; no separate PRD yet) • **Key ADRs:** [ADR-181](../adrs/adr-181-objects-from-any-harness-are-absorbed-quarantined-approved-and-served-back-never-granted.md), [ADR-087](../adrs/adr-087-cross-mem-portable-cross-harness-memory.md), [ADR-092](../adrs/adr-092-cross-mem-continuous-bidirectional-sync.md), [ADR-063](../adrs/adr-063-skill-md-as-generated-artifact.md), [ADR-072](../adrs/adr-072-capability-projection.md), [ADR-114](../adrs/adr-114-mcp-authority-enforcement.md), [ADR-117](../adrs/adr-117-skill-conformance-steward.md), [ADR-032](../adrs/adr-032-standards-positioning-dual-track.md)

## Overview

Cross-mem makes a user's memory portable across agent harnesses. This spec
extends the same four verbs (absorb, migrate, sync, serve) and the same
provenance coordinate to the other things a harness stores that a user would
want to carry: skills, agent definitions, user-defined commands, MCP server
registrations, and sessions. It also fixes the contract for the one new risk
these objects bring that memory did not: they are instruction content that can
drive tool use.

Evidence marks used in every harness-fact table:

| Mark | Meaning |
|---|---|
| `verified-by-run` | Observed on this machine on 2026-10-08 by running the harness or listing its files. Version noted where a binary ran. |
| `from-docs` | Taken from vendor documentation or a prior survey; not observed here. Re-verify before a build depends on it. |
| `unverified` | Believed, not checked, or the harness is not installed here. Never build on it. |

Harnesses present on the reference machine: Claude Code 2.1.291 (ran), OpenCode
1.17.8 (ran), Cursor (app and `~/.cursor` present; `cursor-agent` CLI absent),
Codex (config and data present under `~/.codex`; the launcher points at a
missing application path, so the binary did not run). Absent: Gemini CLI,
`cursor-agent`, Goose (all `unverified` for every fact below).

## 1. Harness storage by object kind

### 1.1 Skills (a folder with a `SKILL.md`)

| Harness | Location | Format | Basis |
|---|---|---|---|
| Claude Code | `~/.claude/skills/<name>/SKILL.md`; plugin bundles carry `skills/<name>/SKILL.md` under `~/.claude/plugins/cache/<marketplace>/<plugin>/<sha>/` | YAML frontmatter (`name`, `description`, optional `license`) + markdown body | `verified-by-run` (files read); project-scope `.claude/skills/` is `from-docs` |
| OpenCode | `.opencode/skill(s)/<name>/SKILL.md`, `~/.config/opencode/skill(s)/<name>/SKILL.md`; **also auto-loads `~/.claude/skills/<name>/SKILL.md` and `~/.agents/skills/<name>/SKILL.md`** | same shape | `verified-by-run` (`opencode debug skill` listed the Claude-synced skills and its built-in guide states the paths) |
| Cursor | built-in skills at `~/.cursor/skills-cursor/<name>/SKILL.md` (same frontmatter); user and project skill locations not observed | same shape | built-in: `verified-by-run`; user/project paths: `unverified` |
| Codex | `~/.codex/skills/` exists and is empty | SKILL.md folders | directory: `verified-by-run`; format: `from-docs` |
| Gemini CLI, Goose | not installed | n/a | `unverified` |
| Axiom | `SkillSpec` in a `SkillRegistry`, rendered to SKILL.md by `skills_emit`; `read_skill_md` / `register_skill_md_dir` read a SKILL.md back (extra keys tolerated, `allowed-tools` parsed, name validated as a dotted identifier) | SKILL.md superset: `name`, `description`, `version`, `inputs`, `allowed-tools`, `generator` | in code: `src/axiom/infra/skill_md.py` |

Observation that shapes the design: the field is already converging on one
file format and two shared folders (`~/.claude/skills`, `~/.agents/skills`).
Copying files between harnesses is therefore not the product. The product is
the control plane over those files: inventory, collision handling, review,
permission narrowing, classification, and one place to see all of it.

### 1.2 Agent (subagent) definitions

| Harness | Location | Format | Basis |
|---|---|---|---|
| OpenCode | `.opencode/agent(s)/<name>.md`, `~/.config/opencode/agent(s)/<name>.md`, or an `agent` map in `opencode.json` | frontmatter fields `name, model, variant, description, mode (primary/subagent/all), hidden, color, steps, options, permission, disable, temperature, top_p`; body is the system prompt; unknown keys fold into `options`. Built-ins: `build`, `plan`, `general`, `explore` | `verified-by-run` (`opencode debug skill` built-in guide; `opencode agent list` ran and listed `build`) |
| Claude Code | `.claude/agents/<name>.md` (project), `~/.claude/agents/` (user); `--agents <json-or-file>` and `--agent <name>` flags exist | markdown with frontmatter (`name`, `description`, `tools`, `model`) | flags: `verified-by-run`; files and fields: `from-docs` (no agents directory on this machine). Note `claude agents` lists running sessions, not definitions (`verified-by-run`); "agent" is an overloaded word, see 4. |
| Cursor | `~/.cursor/agents/` exists and is empty | unknown | directory: `verified-by-run`; format: `unverified` |
| Codex | no agents directory | n/a | `verified-by-run` (absent) |
| Axiom | AEOS `agent` capability kind in `axiom-extension.toml`; `/agents` lists declared agents | manifest + code, not a prompt file | in code |

### 1.3 User-defined slash commands and prompts

| Harness | Location | Format | Basis |
|---|---|---|---|
| Axiom chat | `$AXI_STATE_DIR/commands/*.md` and `<project>/.<cli>/commands/*.md`; project wins | optional frontmatter (`description`, `argument-hint`), body with `$ARGUMENTS`; the module states it mirrors Claude Code's pattern | in code: `chat/user_commands.py` |
| Claude Code | `.claude/commands/*.md`, `~/.claude/commands/*.md` | same shape | `from-docs` (absent on this machine) |
| OpenCode | `command` map in `opencode.json` (`{description, prompt}`); a file form under `.opencode/command/` is likely | JSON entries | map: `verified-by-run`; file form: `unverified` |
| Codex | custom prompts, `~/.codex/prompts/*.md` | markdown | `from-docs` (directory absent here) |
| Cursor | `.cursor/commands/*.md` | markdown | `unverified` |
| Gemini CLI | `.gemini/commands/*.toml` | TOML | `unverified` (not installed) |

### 1.4 Instruction and rules files

Covered by cross-mem: the survey (`docs/reference/harness-memory-survey-2026-07.md`)
and the 13 write-back targets in `src/axiom/memory/sync/writeback.py`
(`RULES_FILE_TARGETS`, in code). One extra observation: Cursor rules observed
on disk are `.cursor/rules/*.mdc` with frontmatter `description` and
`alwaysApply` (`verified-by-run`). Rules files are memory-adjacent and stay
under cross-mem; this spec adds nothing to them except the managed index block
in 2.8.

### 1.5 MCP server registration

Already one matrix: `TOOL_SPECS` in
`src/axiom/extensions/builtins/mcp/install.py` (path, format, detection and
export-control posture per harness; in code). Observed on this machine:
Cursor `~/.cursor/mcp.json` keys `mcpServers`; Codex `~/.codex/config.toml`
table `mcp_servers`; OpenCode `mcp` map with `{type: local|remote, command[],
enabled, env}` (all `verified-by-run`). This matrix is the natural home for the
per-kind object paths in this section (see D8 in the ADR).

### 1.6 Hooks

| Harness | Location | Basis |
|---|---|---|
| Claude Code | `hooks` key in `~/.claude/settings.json`, event name to `[{matcher?, hooks:[{type: "command", ...}]}]`; events seen: `Stop`, `SessionEnd` | `verified-by-run` (keys only; contents not inspected) |
| Others | not examined | `unverified` |

Hooks run arbitrary commands on lifecycle events with the user's authority and
the event vocabularies differ per harness. They are out of scope for portability
(ADR-181 D1): inventoried and reported, never translated or emitted.

### 1.7 Plugin bundles

A bundle is a container, not a kind. Observed: a Claude Code plugin is a
directory with `.claude-plugin/plugin.json` plus `skills/`; the install record
(`~/.claude/plugins/installed_plugins.json`) carries marketplace, version and
`gitCommitSha` (`verified-by-run`). Codex config has `marketplaces` and
`plugins` tables (`verified-by-run`, keys only). Absorb unpacks a bundle into
the object kinds above, keeps the marketplace and commit as provenance, and
imports no bundle code.

### 1.8 Sessions

| Harness | Location | Format | Basis |
|---|---|---|---|
| Claude Code | `~/.claude/projects/<slug>/<session-uuid>.jsonl` | JSONL records | `verified-by-run` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, `archived_sessions/`, plus SQLite state | JSONL | dates tree `verified-by-run`; record shapes read by the `/renew` readers (see 5) |
| Cursor | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` | JSONL | `verified-by-run` (existence); record shape `unverified` |
| OpenCode | SQLite `~/.local/share/opencode/opencode.db`; `opencode export [id]` writes JSON and `opencode import <file|url>` reads it | DB + JSON | commands: `verified-by-run` (help); schema: `unverified` |
| Observation | Codex keeps `~/.codex/external_agent_session_imports.json` listing foreign (Cursor) transcripts it detected | | file: `verified-by-run`; behavior: `unverified` |

### 1.9 Memory

Cross-mem (ADR-087, ADR-088, ADR-092). Not repeated here.

## 2. Contracts

### 2.1 The portable object record

One record shape for every object kind. It is the unit that gets stored,
deduplicated, reviewed, served and exported.

| Field | Meaning |
|---|---|
| `kind` | `skill`, `agent`, `command`, `mcp_server`, `session` |
| `name` | Identifier as the source spelled it, plus `qualified_name` (see 2.4) |
| `description`, `body` | Normalized text: UTF-8, LF, BOM stripped, trailing whitespace trimmed. The body is never rewritten beyond that |
| `declared_tools` | The source's tool grant list verbatim |
| `mapped_tools` | Result of the 2.3 mapping: Axiom capability classes, and what was dropped |
| `arguments` | Declared inputs or argument hint |
| `hints` | Model, mode, temperature and similar. Recorded, never applied (ADR-181 D5) |
| `extras` | Every frontmatter key the mapping did not understand, verbatim, so a return trip to the origin harness restores them |
| `origin` | `SourceOrigin` (`harness`, `account`, `source_ref`, `imported_at`) with `source_ref` = `<scope>:<kind>:<relative path>`; plus the bundle marketplace and commit when present |
| `content_hash` | SHA-256 of the normalized record minus `origin` |
| `classification` | A label from the existing classification vocabulary. Absent until set at approval |
| `trust` | Not a field of the record: an append-only decision record 2.2 |
| `conversion` | The `ConversionReport` of 2.5 |

Storage (ADR-181 D3): the record is written through `CompositionService` as a
fragment of cognitive type `procedural` (`resource` for `mcp_server`), with
`object_kind` in structured content. This inherits write-once provenance,
idempotency on `(harness, account, source_ref)`, entity-resolution dedup, the
serving gate, bundle export and audit with no new store. Objects are excluded
from the recall projection corpus (ADR-088) until approved, and then reach
agents through the library door in 2.6, not through memory recall.

### 2.2 Trust states

Trust is an append-only chain of decision records keyed by `content_hash`, never
a mutation of the fragment (provenance is immutable).

| State | Visible to agents | Callable or listed | Emitted to harnesses | How it is entered |
|---|---|---|---|---|
| `discovered` (the default, "quarantined") | No: inventory row only (name, hash, origin, scan report) | No | No | absorb |
| `blocked` | No | No | No | scan hard-fail (secret found); cleared only by a new content hash |
| `approved` | Yes, on the surfaces named in the approval | Yes, within `mapped_tools` intersect grants | Only to harnesses named in the approval | a human approval record |
| `changed` | The last approved version stays active until re-review decides | | | source content hash differs from the approved hash; the new version is `discovered` |
| `revoked` | No | No | Emitted copies are removed at the next session boundary | a human revocation |

An approval record holds: approver (human principal), content hash, the review
summary shown (2.7), `classification`, the exposure set (surfaces and harnesses),
and `via` (`single` or `bulk`).

### 2.3 Tool-name and permission mapping

Rule: a foreign grant is a request, not a grant. The effective set for an
imported object is `mapped(declared_tools)` intersect the invoking principal's
grants intersect the surface policy (`ToolPermissions`, the ADR-114 identity and
effect gates, site rules). Mapping can only narrow.

| Foreign term (basis `from-docs` unless noted) | Axiom capability class | Result |
|---|---|---|
| `Read`, `Glob`, `Grep`, `LS`, OpenCode `read`/`list`/`glob`/`grep` | `read` | Allowed if the principal holds read |
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit`; OpenCode `permission.edit` (`verified-by-run`) | `write` | Approval category WRITE: ask per call |
| `Bash`, `Bash(prefix:*)`, OpenCode `permission.bash` (`verified-by-run`) | `exec` | Prefix patterns are NOT translated into pattern allows (matching semantics are harness-specific). Mapped to ask per call |
| `WebFetch`, `WebSearch` | `network_read` | Allowed only if the principal and tier allow egress |
| `Task` / subagent spawn | `delegate` | Not granted in phases 1-3 |
| `mcp__<server>__<tool>` | resolved by `(server, tool)` against the registry | Granted only if it resolves to a known capability and passes the ADR-114 gates; unresolved is dropped |
| Any other name | none | Dropped and reported |
| A deny or `deny` entry | deny | Carried over (narrowing is always safe) |
| `allow`-all wildcards (OpenCode `permission: "*" allow` appears in its default `build` agent, `verified-by-run`) | none | Never imported as a grant |

A skill body that tells a model to run a command grants nothing: the call is
gated where it happens. Quarantine exists because the text steers whatever the
model is already allowed to do (prompt injection by a third party's
instructions), not because the object holds authority.

### 2.4 Names, collisions, precedence

Identity key for dedup: `(kind, normalized name)`; version identity: `content_hash`.

| Case | Treatment (same three tiers as ADR-087 D3) |
|---|---|
| Same name, same hash, different harnesses | **Exact**: one canonical record with several origins |
| Same name, differs only in frontmatter order, whitespace, harness-specific extras | **Near-duplicate**: merged as variants, reversible, difference shown |
| Same name, different substance | **Conflict**: both kept as `name@<harness>`, flagged, never auto-merged; the user picks a canonical one |

Precedence for names in Axiom surfaces: registry commands and built-in or
extension skills outrank everything; an imported object can never shadow a
registered command, a built-in skill or an MCP tool name, and takes a qualified
name `name@<harness>` on collision. Among imported objects, project scope
outranks user scope (the same rule `user_commands.py` uses). Axiom never alters
a harness's own precedence inside that harness.

### 2.5 Conversion fidelity and its report

Every conversion emits a `ConversionReport`:

| Field | Values |
|---|---|
| `fidelity` | `exact` (same format family, byte-identical after normalization), `structural` (all fields mapped, comments and ordering normalized), `lossy` |
| `dropped` | Fields with no target equivalent (e.g. temperature into a harness without one) |
| `approximated` | Fields mapped with changed meaning (a permission block into a tool list; a model name into a different provider's alias) |
| `widened` | Must always be empty. A non-empty value is a bug and fails the build |

The report is stored with the record, shown at approval and by
`axi harness show`, and counted in listings (`3 lossy`). Round-trip law, tested
as a fixed point (the ADR-117 method): `absorb(emit(absorb(x), origin)) ==
absorb(x)` and, for same-family pairs, `emit(absorb(x), origin) == normalize(x)`.

### 2.6 The adapter capability and the verbs

Adapters keep `scan()` for memory fragments. A second optional capability, the
same pattern `/renew` uses for sessions (`SessionCapable`), reads objects:
`scan_objects(kinds) -> ObjectScan` (candidates plus skipped-with-reason). Read-only
against sources, no network, no execution, YAML via safe load only.

| Verb | Skill function (ADR-056) | Notes |
|---|---|---|
| absorb | `harness.scan`, `harness.absorb` | scan is inventory only; absorb writes `discovered` records |
| review | `harness.show`, `harness.diff` | pure reads |
| approve / revoke | `harness.approve`, `harness.revoke` | human principals only (ADR-181 D4) |
| serve | `harness.emit` | writes only Axiom-owned files, session-boundary cadence |
| sync | managed service, later phase | detect source change, produce `changed`, never auto-approve |

CLI noun `axi harness` (purpose-named per ADR-056). Chat and MCP are
projections of the same functions (ADR-072).

### 2.7 What the reviewer sees

Provenance (harness, account, path, bundle, marketplace, commit, install date);
whether Axiom or a third party appears to have authored it (path under a plugin
cache means third party); the diff against the last approved version; foreign
tool request and the effective Axiom set; scan findings (secret patterns via
`looks_like_secret`; zero-width and bidirectional control characters; injection
phrases; URLs and fetch-then-execute shapes; base64 blobs; bundled executables
listed by name and hash, never imported); proposed classification; the surfaces
and harnesses it would be exposed to; the conversion report. Scan findings
advise; the human decision and the labels decide (the ADR-152 posture).

### 2.8 Serving

Three doors, all behind the one serving gate (ADR-087 D7), fail-closed.

| Door | Mechanism | Notes |
|---|---|---|
| Native files | `harness.emit` writes `axi-<name>` entries into the target harness's skill or command location | Only locations marked `verified-by-run` in the harness descriptor are written by default. Axiom-owned by path prefix; echo is suppressed by a content-hash index (ADR-092 D1 method), so emitted copies are never re-absorbed |
| MCP | `skills.get` and `prompts` (already surface-projected for any SKILL.md-derived capability; prompts also reach clients through the existing prompts-as-tools door, `mcp/aggregation.py`) | The bridge for harnesses with no file location, or none verified |
| Index block | A second managed block in AGENTS.md (`axiom:skills`) listing approved names with one line each and how to fetch the body | Progressive disclosure: names in the cached prefix, bodies on demand; same session-boundary cadence rule as cross-mem write-back |

An object may always be served back to its origin coordinate. Serving to any
other harness, account or deployment tier requires a classification label whose
policy admits that coordinate; unlabeled is denied. Emission honors the
`model_routable` flag in the harness descriptor: a restricted object never goes
to a harness whose model path is outside the controlled tier (fail-closed for
every harness marked unverified, as `TOOL_SPECS` does today).

## 3. The sweep of the chat command set

Source: the registry (`chat/command_registry.py`, ADR-168) on
`feat/chat-commands-and-placement` plus the per-command branches
`feat/chat-cmd-*`, `feat/chat-renew`. `/accept` and similar names found in the
signals CLI are not chat commands and are excluded. The `expose` field
(`chat` | `mcp` | `harness-command-file`) is not in the registry yet; this table
is its first population.

Classes: **portable object** (the thing it manages is shared across harnesses),
**cross-harness view** (reads or aggregates several harnesses), **bridge**
(already or easily reached from other harnesses through MCP tools and
instruction files), **chat-only** (meaningless outside the chat loop).

| Command | Class | Reasoning | Follow-on |
|---|---|---|---|
| `/help` | chat-only | Lists this chat's commands | Gains an `exposed to` column once `expose` exists |
| `/exit` | chat-only | Ends this loop | none |
| `/clear` | chat-only | Clears this transcript | none |
| `/new` | chat-only | Starts a native session | none |
| `/image` | chat-only | Attaches to the next message in this loop | none |
| `/model` | chat-only | Provider switch with placement and tier checks is chat state | none |
| `/where` | chat-only | Reports this chat's model and retrieval placement | none |
| `/status` | chat-only | This session's info | none |
| `/context` | chat-only | This session's window use | none |
| `/stats` | chat-only | Per-turn latency and cost for this loop | none |
| `/trace` | chat-only | Admin per-turn trace | none |
| `/planes` | chat-only | What this chat is connected to | none |
| `/compact` | chat-only | Each harness has its own compaction | none |
| `/rename`, `/archive` | chat-only | Operate on native sessions | none |
| `/fork`, `/diff`, `/undo` | chat-only | Depend on this chat's edit checkpoints | none |
| `/copy` | chat-only | Clipboard of this loop | none |
| `/advisor` | chat-only | Post-turn tips for this loop | none |
| `/tasks` | chat-only | Background tasks of this loop | none |
| `/update` | chat-only | Updates Axiom itself | none |
| `/compare` | chat-only | Two models in this loop | none |
| `/sources` | chat-only | Grounding of the last reply | none |
| `/plan`, `/ask`, `/agent`, `/mode` | chat-only | Interaction mode is enforced by this loop. OpenCode ships `plan` and `build` agents (`verified-by-run`), so the idea exists elsewhere, but the enforcement does not travel | Out of scope; revisit only if agent definitions (phase 3) can carry `mode` faithfully |
| `/rag` | bridge | Toggle is chat-only, but retrieval itself is the MCP tool `axiom_rag__retrieve` | Nothing for the toggle; the tool is already the bridge |
| `/memory` | bridge | The portable object here is memory, already handled; harnesses reach it through the memory MCP tools and rules files | Emit as `/axi-memory` command file (capability mode) |
| `/save` | bridge | Writing a reply to the corpus equals `axiom_memory_append` from any harness | Emit as `/axi-save` (capability mode) |
| `/signal` | bridge | `axiom_signals__brief` exists | Emit `/axi-brief` (capability mode) |
| `/alerts` | bridge | Notification MCP tools exist | Emit `/axi-alerts` (capability mode) |
| `/pub` | bridge | Press MCP tools exist; write tools stay behind approval (ADR-114) | Emit read-only `/axi-pub-status` only |
| `/init` | bridge | Writes the AGENTS.md managed block, which is the cross-mem write-back door | Keep chat-only command; document that `axi memory` write-back is the cross-harness form |
| `/review` | portable object | Already a prompt template (`templates/review.md`, `$ARGUMENTS`, `$DIFF`, user override files). A command file is a prompt, so this is the model case | Emit `/axi-review` to every verified harness; first emission target |
| `/mcp` | cross-harness view | Already lists the servers Axiom provides, which harnesses use them and export-control posture | Add per-kind object registration status from the harness descriptor |
| `/doctor` | cross-harness view | `axi doctor` already consumes the per-harness registrars; health across harnesses is the natural extension | Add: objects emitted vs present, drift, `changed` and `blocked` counts |
| `/sessions` | cross-harness view | Browse sessions across harnesses, filter by harness | Rides `feat/chat-renew` readers (Claude Code, Codex); add Cursor and OpenCode readers |
| `/resume`, `/renew` | cross-harness view and portable object (session) | Same handler; importing a foreign session creates a new native session with provenance | See journey J2; lossy report counts shown on import |
| `/export` | portable object (session, outbound) | Writes md or json; a harness-importable form is possible (OpenCode `import` reads JSON, `verified-by-run`) | Not before phase 5; needs the OpenCode schema verified |
| `/usage` | cross-harness view (future) | Token and cost per harness is what a user wants; OpenCode has `stats` (`verified-by-run`), Claude keeps a stats cache file (`verified-by-run`, contents not read) | Phase 5 candidate; read-only readers |
| `/permissions` | cross-harness view (read) | Importing permissions would widen, so never imported (2.3). A comparison of what each harness would allow per capability class is useful | Phase 5 candidate; deny-only sync is an open question |
| `/tools` | chat-only | The chat's offered tool table | Cross-harness equivalent is the MCP tool listing in `/mcp` |
| `/skills` | portable object (skill) manager and cross-harness view | Today lists the registry; becomes the inventory including `discovered`, `blocked`, `changed` | Phase 1: add `--from <harness>`, state column, and the review and approve entry |
| `/agents` | portable object (agent) manager and cross-harness view | Today lists declared Axiom agents | Phase 3: same treatment as `/skills`; keep "agent" disambiguated (4) |
| `/harness` (new) | cross-harness view | One entry for status, the approval queue and emit state | Thin wrapper of `axi harness status` |

Emitted-as-command-file set (initial): `/review` (template mode), then
`/memory`, `/save`, `/signal`, `/alerts` (capability mode), named `axi-<name>`.

How emission is generated from the registry declaration. Today a `Command`
carries `name, summary, group, handler, usage, subcommands, aliases, hidden,
surfaces, mutates, requires, complete`. Add:

| Field | Meaning |
|---|---|
| `expose` | Subset of `chat`, `mcp`, `harness-command-file`; default `{chat}` |
| `template` | Path of a prompt body (with `$ARGUMENTS`). Present for template commands; the chat handler renders the same file, so there is one source |
| `capability` | Registry capability name for commands that are a call (`signals.brief`). Emission renders "call tool X and summarize" with the tool name from `capability_projection` for the target surface |
| `cli` | The `axi` verb with the same parameters. Handlers become skill functions (ADR-056); slash form, CLI form and MCP tool take their parameters from the one schema, which is the CLI flag parity |

A command with `handler` only (Python logic, no `template` or `capability`)
cannot be emitted: a command file is a prompt, not code. `expose` containing
`harness-command-file` without one of `template` or `capability` fails registry
construction (the same style of check `register` already applies to names). The
generator renders per-harness: filename `axi-<name>.md` (or `.toml` where the
target requires), frontmatter `description` from `summary` and `argument-hint`
from `usage`, body from `template` or the capability stub. It refuses a name the
target harness descriptor lists as built-in, so an emitted file can never shadow
a built-in. The MCP-name collision work (`test/mcp-harness-collisions`, no
commits yet as of this writing) fixes how `mcp__<server>__<tool>` names relate to
slash commands in each harness; emitted names reconcile with it when it lands.

## 4. Terminology guard

"Agent" names four things: an Axiom manifest-declared agent (AEOS), a harness
subagent definition (a prompt file), a running harness session (what
`claude agents` lists), and the chat's interaction mode `/agent`. UI and docs say
"agent definition" for the second and "session" for the third.

## 5. Sessions track

Owned by `feat/chat-renew` (read-only here): `SessionCapable` readers
(`list_sessions`, `load_session`) for Claude Code and Codex; tool calls kept as
text and counted in a `lossy` figure; tool-result content not copied (largest
secret risk). This spec only requires of it: the imported session is a NEW
native session carrying `SourceOrigin`; the source is untouched; the same trust
rule applies (imported assistant text is data, not instructions, and a resumed
foreign session starts with tools at the destination's current grants).

## 6. User journeys

Each result below ends with its next step.

### J1: "Bring my skills with me"

1. `axi harness scan` prints an inventory: per harness, skills / agent
   definitions / commands found, how many names collide across harnesses, how
   many are third-party (plugin cache), how many fail the scan.
   Next: `3 collisions, 2 blocked (secret pattern) - axi harness show --collisions`.
2. `axi harness absorb --kind skill --from claude-code` creates `discovered`
   records, idempotent. Next: `41 imported, 0 active. Review: axi harness review`.
3. `axi harness review code-review@claude-code` shows the 2.7 summary and a
   proposed label. Next: `approve | edit label | skip | block`.
4. `axi harness approve code-review --expose chat,mcp,opencode` records the
   approval. Next: `registered as skill code-review; emitted to opencode;
   try /skills or run axi doctor`.
5. Later the source file changes. The user is told at the next session start:
   `code-review changed in claude-code (diff attached); still serving the
   approved version. Review: axi harness review code-review`.

### J2: "Continue that session in another tool"

1. `/sessions --harness codex` (or `/renew` with the filter) lists sessions
   across harnesses, newest first, with harness and age.
2. Selecting one imports it as a NEW native session with a banner naming the
   origin and the lossy count (`14 tool calls kept as text, 9 results not
   imported`). Next: `continue here, or /memory to keep what mattered`.
3. If the active model tier would not be admitted for the imported content, the
   renew flow already says so; the next step offered is `/model` to pick an
   admitted provider.
4. At the end of the session the memory capture path (cross-mem) offers the
   durable facts for the ledger. Next: `axi memory show`.

### J3: "Write a command once, have it everywhere"

1. The user edits `review.md` in their Axiom commands directory (the override
   mechanism that exists today).
2. `axi harness emit --kind command --to opencode,claude-code` previews the
   files it would write (`axi-review.md` in each), each with its conversion
   report, and the names it refused (collision with a built-in).
   Next: `apply? y/n`.
3. Applied at the session boundary. `axi doctor` now shows `/axi-review present
   in 2 of 2 harnesses, in sync`.
4. A harness is later missing the file (user deleted it). `axi doctor` flags
   drift. Next: `axi harness emit --repair`.

## 7. Phasing (each phase ships on its own)

| Phase | Deliverable | Why it stands alone | Depends on |
|---|---|---|---|
| 0 | Harness descriptor: extend the `TOOL_SPECS` matrix with per-kind object locations, built-in names and a `verified` flag per fact. `axi harness scan` read-only inventory with collisions and scan findings. No import | The user finally sees what they have across tools, and duplicates; zero write risk | none |
| 1 | Skill absorb, quarantine, review, approval, SkillSpec registration, `/skills` states, MCP `skills.get`, hash pinning, `changed` handling | Skills usable in Axiom chat and over MCP with gates, nothing written to any harness | phase 0; `read_skill_md` exists |
| 2 | Serve: `harness.emit` for skills and commands to verified locations, the `axiom:skills` index block, registry `expose`/`template`/`capability`/`cli`, `/review` first | Write once, run in many harnesses | phase 1 |
| 3 | Agent definitions: absorb, mapping (2.3), conversion reports, emit; running a foreign definition inside Axiom chat is a separate second approval | The lossy, permission-heavy kind, shipped last on proven machinery | phase 2 |
| S | Sessions: `/renew` across harnesses, more readers (Cursor, OpenCode) | Independent track; already in flight | `feat/chat-renew` |
| 4 | Continuous sync service: watch sources, `changed` queue, outbound repair, cross-harness `/usage` and `/permissions` views | Convenience on top of a complete manual path | phases 1-2 |

Not planned: hooks, bundle code, secrets or credentials in any object, foreign
permission grants.

## 8. Standards positioning

SKILL.md is the interchange format for skills and the portable object's skill
form is a SKILL.md superset that stays valid under the public format (extra keys
are tolerated by the readers observed; unknown keys fold into `options` in
OpenCode, `verified-by-run`). AGENTS.md remains the instruction-file bridge and
hosts the index block. MCP prompts and tools are the bridge for commands and for
harnesses with no file location; two of three harnesses measured never call
`prompts/list`, so the tool door stays (in code, `mcp/aggregation.py`). No public
standard covers agent definitions that we have adopted; the formats seen are
per-harness (ADR-032 lists candidates and defers). Per ADR-032, AEOS is not
promoted externally: nothing emitted to a harness names AEOS, and the portable
object record is described here in terms of the public formats. The trust model
(quarantine, hash-pinned approval, narrowing-only mapping) is the Axiom
differentiator and is described as behavior, not as a standard.

## Open questions

See the decision table at the top of ADR-181; each has a recommended default
and an owner (Ben).

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
