# Agent Extension Open Standard (AEOS) — Version 0.1

**Status:** Draft — Internal (per ADR-032 dual-track strategy; intended for eventual public release pending strategic decisions)
**Version:** 0.1.0 (0.2 draft amendments: §4.11 chain readiness, §4.12 recipe; additive, see §4.11.1)
**Editor:** Benjamin Booth
**Date:** 2026-04-21
**Reference implementation:** Axiom platform (b-tree-labs/axiom-os)

---

## Abstract

The Agent Extension Open Standard (AEOS) defines a portable, signed, cross-harness package format and runtime contract for agentic AI extensions. An AEOS-conformant extension bundles one or more capability kinds — agents, tools, CLI commands, services, adapters, skills, hooks, and prompts — under a single manifest with declared dependencies, standardized test bases, and cryptographic provenance. AEOS wraps and extends existing industry standards (MCP, A2A, SKILL.md, AGENTS.md, OpenAPI, Sigstore) rather than replacing them. Extensions using only AEOS's public-standard subset are interoperable with any harness implementing those standards; extensions using AEOS's federation, attestation, and governance features require an AEOS-conformant runtime.

AEOS exists because no existing standard covers the full extension packaging need. MCPB bundles MCP servers only. SKILL.md packages single skills only. OASF schematizes metadata but does not specify code layout or signing. Claude Code's plugin format is scoped to one harness. AEOS unifies these concerns in a single published specification.

---

## 1. Status and Publication

AEOS is maintained as an internal specification per ADR-032. It is:
- Authoritative for Axiom, Keplo, Vyzier, and consumer extensions
- Not advertised externally until a strategic decision to publish
- Written to publishable quality so that release decisions can execute quickly
- Versioned independently of any single product (AEOS 0.1, 0.2, 1.0)

Transitions of AEOS content to public standards bodies (AAIF, OASF, MCPB contributions) follow the triggers defined in ADR-032.

---

## 2. Relationship to Existing Standards

AEOS does not replace any existing standard. It composes them:

| Existing Standard | AEOS Relationship |
|---|---|
| **MCP (Model Context Protocol)** | Tools within AEOS extensions expose themselves as MCP tools when appropriate. AEOS extensions can be loaded as MCP servers for MCP-only clients. |
| **MCPB (MCP Bundle)** | An AEOS extension whose only declared capability is one or more MCP tools is a valid MCPB archive with additional AEOS metadata. MCPB-only clients ignore the extra fields. |
| **MCP Registry / `server.json`** | AEOS registry entries are a profile of the MCP Registry's `server.json` schema (reverse-DNS identity, `environmentVariables[]{isSecret}`), with Axiom-specific fields in the reverse-DNS `_meta` namespace. An AEOS registry is a **sub-registry** of the MCP Registry (consume-only). See §13.5–13.6. |
| **A2A (Agent-to-Agent Protocol)** | AEOS agents expose A2A Agent Cards at `/.well-known/agent-card.json` when running in network-reachable mode. |
| **SKILL.md / agentskills.io** | AEOS skill capabilities use SKILL.md format verbatim; an extension's `skills/<name>/SKILL.md` files are valid standalone skills. |
| **AGENTS.md (AAIF)** | AGENTS.md — the repository-level context file stewarded by the Agentic AI Foundation under the Linux Foundation — is AEOS's canonical, hand-authored project-context document. An AEOS repository authors exactly one such file. Every harness-specific context file (`CLAUDE.md`, `GEMINI.md`, `.cursor/rules/*`, `.junie/guidelines.md`, `.github/copilot-instructions.md`, …) MUST be either a symlink to `AGENTS.md` or an artifact generated from it carrying a do-not-edit marker, with drift detection enforced in CI. Reference implementation: the `context` extension (`axi context sync` / `check`). |
| **OpenAPI 3.1** | AEOS adapter capabilities that wrap REST/HTTP integrations declare their surface via embedded OpenAPI specs. |
| **OASF (Open Agentic Schema Framework)** | AEOS manifest fields are compatible with OASF's capability and metadata schemas where overlap exists. Proposals to add non-overlapping fields to OASF may be submitted per ADR-032. |
| **Sigstore / PEP 740** | All AEOS extension releases are signed via Sigstore's keyless flow. |
| **Semantic Versioning 2.0.0** | All AEOS extensions follow semver strictly. |
| **Keep a Changelog** | All AEOS extensions maintain CHANGELOG.md in the Keep-a-Changelog format. |
| **MADR (Markdown Architecture Decision Records)** | AEOS extensions use MADR for extension-level architecture decisions. |

---

## 3. Foundational Principles

AEOS is designed against seven principles. Every later specification detail derives from these.

### 3.1 Self-containment

An AEOS extension is a single directory with all artifacts it owns: source code, tests, documentation, manifest, changelog, license. Extraction from a monorepo to a standalone repository requires no restructuring — just moving the directory. Per ADR-031.

### 3.2 Purpose-driven naming

Extensions are named by what they do or which domain they serve, not by what type they are. Type information lives in the manifest, not the directory name. There is no `_agent`, `_tool`, or other type suffix.

### 3.3 Compound by default

Every extension is scaffolded with the canonical compound layout (typed subdirectories for each capability kind). Extensions that ultimately provide only one capability kind are compound extensions with only one populated subdirectory. This uniformity removes the decision burden at authoring time. Conformance requires a kind's subdirectory only when the manifest provides that kind — the scaffold creates all of them for convenience, authors delete the unused ones, and empty ceremony directories are neither required nor checked (lint rule AEOS040 enforces provided kinds only).

### 3.4 Deterministic trust boundary

AEOS enforces the deterministic/model-mediated boundary (per Axiom `spec-security.md §2`). LLMs may advise; deterministic code authorizes. AEOS trust profiles, RACI gates, and classification stamps are all deterministic primitives.

### 3.5 Capability declaration via entry points

Capabilities are declared distributively — via Python entry points, registered through module imports. The manifest enumerates provided capabilities for discovery and validation; the registration is Python-native, allowing refactors without manifest churn. The manifest is authoritative for validation; the Python entry points are authoritative for loading.

### 3.6 Signed releases by default

Every AEOS release is signed via Sigstore's keyless OIDC flow. Installers verify signatures before executing extension code. Unsigned extensions install only with explicit `--allow-unsigned` override. This is a direct response to the ClawHavoc incident.

### 3.7 Federation-native where applicable

AEOS extensions can participate in multi-institutional federation: signed attestations travel with them, trust-profile compatibility is declared, and quarantine/recovery ceremonies are first-class. These features are optional — a local-only extension is a valid AEOS extension that ignores federation metadata.

---

## 4. Capability Kinds

AEOS defines exactly eight core capability kinds (§4.1-§4.8). This list is extensible by future AEOS versions but closed within a given version. The schema additionally enumerates runtime-consumed kinds that sit outside the core list — `signal_type`, `safety_check`, `api` (§4.10) and `channel_adapter` — see §6.

> **Point-release correction.** `prompt` (§4.8) was formalized in a 0.1.x point
> release and this paragraph was not updated with it, so the document said seven
> while defining eight. A reader asking whether they could declare a prompt got
> "no" from the prose and "yes" from §4.8 — and the implementation sided with
> the prose: the MCP aggregator read an extension's declared prompts only to
> decide whether that extension was worth including, then merged none of them,
> so a live server advertised the `prompts` capability and served an empty list.
> AEOS 1.0 §4 already states eight; this brings 0.1 into agreement with both its
> own §4.8 and its successor.
>
>
> **Second point-release correction (2026-09-18).** `api` shipped the same way:
> verbs have been declaring `kind = "api"` to join the composed `/api/v1` surface,
> and the manifest schema rejected it — so an extension that followed the
> documented contribution mechanism failed its own conformance test. The schema now
> carries `ProvidedApi` and §4.10 defines the kind. It is NOT a ninth core kind: it
> is runtime-consumed, enumerated alongside `signal_type`, `safety_check` and
> `channel_adapter` in §6, and the core count stays at eight.

> **0.2 draft addition.** `recipe` (§4.12) is a runtime-consumed kind alongside
> `signal_type`, `safety_check`, `api` and `channel_adapter`; the core count stays
> at eight.

### 4.1 agent

An LLM-backed autonomous component with persistent identity and state across sessions. Agents have names in the Axiom AXI convention (ALL-CAPS-HYPHEN: AXI, SCAN, TIDY, PRESS, TRIAGE, CURIO, CHALKE, WARDEN).

- Provides: `classify`, `plan`, `execute`, `learn` (depending on agent role)
- Declares: `name`, `description`, `allowed_tools`, `uses_skills`, `requires_signals`, `state_model`
- Roster fields (optional; every surface that describes the agent reads them, see `axiom.agents.roster`): `role` (one line, e.g. "secret steward"), `domain` (what it owns, in a person's words), `namespaces` (the skill namespaces it owns: they bound its delegated toolset and put its `[NAME]` tag on those tools' descriptions), `also_owns` (extensions it owns beyond its host), and `asks` (example requests)

> **Agent action safety:** any agent op that mutates external state (close a GitHub issue, delete a branch, publish a wheel, send a notification) MUST route through `axiom.policy.agent_action_guard.guarded_act`. The framework composes hard-disable, sentinel-pause, state preconditions, volume bound, and dry-run. Operators get a uniform kill-switch surface (`axi <agent> pause`) and a uniform env-var schema (`<AGENT>_<OP_CLASS>_DISABLE / _DRY_RUN / _MAX_PER_TICK`). See [`spec-agent-action-guard.md`](spec-agent-action-guard.md) for the contract, the worked example, and patterns to avoid.
- Exposes: A2A Agent Card when networked
- Agent persona (system prompt, role definition) lives as `agents/<name>/persona.md` inside the extension — this is internal to the agent and is NOT a standalone skill

**Agents and skills are distinct capabilities.** An agent may invoke skills, but skills are not bound to a specific agent (see §4.6). An agent's `uses_skills` manifest field lists which skills it may invoke; the listed skills remain standalone and available to other agents.

### 4.2 tool

A stateless callable with typed input and output schemas. Tools are invoked by agents or CLI commands and do not maintain state across calls.

- Provides: a single `invoke(input) -> output` callable
- Declares: `name`, `description`, `input_schema`, `output_schema`, `side_effects`, `idempotent`
- Exposes: MCP tool manifest when networked via MCP server

When a tool is the MCP (or agent-tool) **projection of a registered
capability** (the common case), its name and input schema are *derived
by the shared projector* (§4.9), not authored here — the capability's
`SkillSpec` is the source of truth (ADR-056/072). A standalone `tool`
block authoring its own schema is reserved for capabilities not backed
by a `SkillRegistry` entry.

### 4.3 cmd

A CLI noun-verb grouping that extends `axi` or a consumer CLI (`neut`, `keplo`).

- Provides: one or more subcommands
- Declares: `noun`, `description`, `subcommands[*]`, `flags[*]`
- Uses: argparse (or framework equivalent) with decorator registration

#### 4.3.1 Verb grammar (normative)

The `noun` declared in a `kind=cmd` block names the **command surface**;
the verbs registered under it (typically as argparse subparsers) name
the **actions** the user takes against that surface. Honoring the
grammar makes commands readable, completable, and uniformly shaped
across the platform.

**Verbs MUST be imperative actions in English.** Examples that conform:
`add`, `assess`, `audit`, `check`, `clean`, `clone`, `create`, `delete`,
`diagnose`, `diff`, `export`, `generate`, `init`, `install`, `invite`,
`join`, `leave`, `lint`, `list`, `migrate`, `monitor`, `prune`, `pull`,
`publish`, `purge`, `receive`, `register`, `remove`, `resolve`, `review`,
`revoke`, `search`, `share`, `show`, `start`, `stat`, `stop`, `submit`,
`sweep`, `sync`, `unregister`, `unwatch`, `upgrade`, `validate`, `watch`.

**Verbs MUST NOT be plain English nouns naming the resource being
acted on.** A bare resource name as a verb (`worktrees`, `vitals`,
`heartbeat`, `patterns`) reads as a sentence fragment ("axi tidy
worktrees" — *what about the worktrees?*), breaks tab-completion
discoverability, and prevents the multi-resource composition pattern
specified in `spec-axi-cli.md §UI Affordances → Encoded conventions`.

**Resources appear as positional arguments to the verb** —
`axi hygiene stat worktrees` (verb=`stat`, resource=`worktrees`) — or as
flags (`axi hygiene prune --worktrees`).

**Reserved verb-shaped nouns (grandfathered).** A small set of
common CLI conventions are accepted as verbs even though they're
nominally nouns, because the convention is established and the
intent is unambiguous:

- `status` — snapshot/umbrella view (cf. `git status`, `kubectl status`)
- `help` — built-in
- `version` — built-in
- `up` / `down` — service lifecycle (cf. `docker compose up`)
- `ls` — abbreviation of `list`

Any addition to the reserved list MUST be motivated by an established
external CLI convention and recorded in the working-doc audit (see
below).

**Lint enforcement.** `axi ext lint --strict` flags verb-grammar
violations as errors (default `--lint` reports them as warnings, to
preserve incremental migration). The lint rule reads each `kind=cmd`
block's entry, calls its `build_parser()`, walks the subparser tree,
and checks each verb name against the imperative-verb set + reserved
list. Verbs flagged but believed-correct can be marked with a
manifest-level escape hatch (`verb_overrides.<verb>.grammar = "approved"`)
that requires a brief rationale string.

**Audit + migration.** A point-in-time audit of the current builtin
extensions is in `docs/working/cli-verb-grammar-audit-2026-05-02.md`.
Migration is incremental — pre-public-launch we refactor cleanly per
`feedback_no_backward_compat_shims` (rename verbs and update all call
sites in one commit; no deprecation aliases).

### 4.4 service

A long-running daemon (scheduler, worker, server, watcher).

- Provides: `start`, `stop`, `status`, `health_check`
- Declares: `name`, `description`, `ports`, `processes`, `deployment_profile`
- Runs: via platform service manager (launchd, systemd, Windows service)

### 4.5 adapter

A third-party integration (IdP, LMS, messaging channel, storage provider, compute target).

- Provides: a Connection-like interface per Axiom Connection framework
- Declares: `integration`, `auth_methods`, `capabilities`, `classification_ceiling`
- Exposes: OpenAPI spec embedded when wrapping a REST service

### 4.6 skill

A `kind = "skill"` block declares an **executable capability** — a
function registered in `axiom.infra.skills.SkillRegistry`,
`(params, ctx) -> SkillResult` with a typed `inputs` schema (ADR-056).
This is the invocable unit; it is the single source of truth that
projects onto the CLI verb, the MCP tool, the agent-facing LLM tool, and
the SKILL.md (§4.9, ADR-072).

Its companion **SKILL.md** — an agentskills.io-compliant directory with
YAML frontmatter + Markdown body + optional scripts/references/assets —
is the *generated* documentation artifact (ADR-063), not the invocation
contract. Do not conflate the two (§4.9.5): the capability is code; the
SKILL.md is instructions an LLM reads.

- Provides: the registered capability function; a generated SKILL.md at `path`
- Declares: `name`, `entry`, `path`, `inputs`, `side_effects`, `idempotent`,
  `surfaces`, `exposed_to_agents` (§4.9); SKILL.md frontmatter carries
  `description`, `license`, `compatibility`, `allowed-tools`
- Format: SKILL.md follows the agentskills.io specification verbatim

**Skills are standalone and reusable.** A skill is NOT tied to a specific agent. Any agent with access to the skill may invoke it. This matches the convention in Anthropic Claude Code, Hermes Agent, and the agentskills.io specification: skills are organization- or project-level resources, consumable by any compatible agent at the time of a relevant task.

If an extension provides both an agent and a skill, they are declared as separate `[[extension.provides]]` blocks. The agent may reference the skill via its `uses_skills` field (see §4.1), but this is a "may invoke" relationship, not ownership.

### 4.7 hook

A lifecycle interceptor at extension or platform boundary (pre-install, post-load, pre-invoke, post-invoke, pre-federation, post-federation).

- Provides: a function matching the hook signature
- Declares: `events[]`, `priority`, `fail_mode` (abort, warn, ignore)
- Use cases: audit logging, rate limiting, classification enforcement, validation

Hook event names are NATS-shape subjects: dot-separated lowercase tokens (`[a-z0-9_]+`), with `*` matching exactly one token and `>` matching one or more tokens at the tail (e.g. `tool.post_invoke`, `tool.*`, `federation.peer.>`). See [`spec-event-bus.md` §5](spec-event-bus.md) for the full grammar.

### 4.8 prompt

A templated MCP prompt the Axi MCP server publishes to external
harnesses (Claude Code, Cursor, Codex, OpenCode, …) as part of the
**cross-harness gravity** mechanism specified in
[`spec-axi-cli.md §Cross-harness gravity → L2`](spec-axi-cli.md#l2--server-published-prompt-templates-the-highest-leverage-lever).

- Provides: a markdown file with templated prompt content
- Declares: `name`, `path`, optional `description`, `arguments[*]`,
  `extends` (parent prompt to fill into), `fill_point`,
  `template_dialect`, `tier`

#### 4.8.1 Naming

Prompt names use a single `.` as the separator between an
**extension namespace** and a **local name**:

- **Platform prompts** (published by Axiom core): `axi-<name>` —
  e.g. `axi-cli-gravity`, `axi-help-snapshot`, `axi-recent-history`.
  Reserved.
- **Extension prompts**: `<extension>.<localname>` — e.g.
  `classroom.grading-context`, `mo.recent-stats`, `federation.peer-summary`.
  Namespaced by the extension's manifest `name`.

The pattern `^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$` is enforced by
the AEOS manifest schema.

#### 4.8.2 Templating dialect

The default dialect is `axiom-mcp-template-v1`, which supports:

- **Argument substitution**: `{{ arguments.cohort_id }}` — substitutes
  values from the `prompts/get` call's arguments map.
- **Platform context**: `{{ context.user.tier }}`, `{{ context.user.id }}`,
  `{{ context.installed_extensions }}`, `{{ context.cwd }}` —
  substitutes live state read from the same `ExtensionRegistry` and
  `competency.json` the CLI uses.
- **Fill points**: `{{ fill: <name> }}` — declared in platform
  prompts; extension contributions target these (see §4.8.3).
- **Conditional sections**: `{{# if ... }} ... {{/ if }}` — rendered
  only when the condition is true at request time.

The dialect is intentionally narrow. Anything more expressive than
the above belongs in CLI verbs, not in prompt templates.

#### 4.8.3 Composition: extending a parent prompt

An extension contributes to a platform prompt by declaring `extends`
+ `fill_point`:

```toml
[[extension.provides]]
kind = "prompt"
name = "classroom.grading-context"
path = "prompts/grading-context.md"
description = "Active class cohort + current rubric for grading workflows"
extends = "axi-help-snapshot"
fill_point = "extension_context"
arguments = [
  { name = "cohort_id", required = true },
  { name = "rubric_version", required = false }
]
tier = "core"
```

The parent platform prompt declares the named fill point in its
template (`{{ fill: extension_context }}`). At `prompts/get` time,
the renderer:

1. Reads the parent template
2. Collects all extension contributions targeting each fill point
3. Filters by the user's tier and per-extension familiarity
4. Composes parent content with extension contributions in
   alphabetical-by-extension order (deterministic; consistent with
   the platform's broader conflict resolution)
5. Returns the composed result to the harness

**Extensions cannot inject content outside declared fill points.**
That's the structural enforcement: while we cannot validate the
*natural-language quality* of an extension's prompt content, we can
ensure that content lands only at points the parent prompt
explicitly invited.

#### 4.8.4 Provenance tagging

Every extension contribution is rendered with a leading provenance
marker so the LLM can attribute content correctly:

```
<!-- contributed-by: classroom (v1.2.0) -->
…the actual prompt content…
<!-- end contributed-by: classroom -->
```

The marker is part of the rendered output, not stripped. Receiving
harness LLMs treat the marker as voice-attribution, the same way
the chat speaker prefix (`AXI ▸`, `TIDY ▸`) signals which agent
is speaking.

#### 4.8.5 Lint expectations

`axi ext lint` enforces what *can* be enforced structurally on
prompt contributions, even though pure semantic enforcement of
natural-language content is out of reach:

- Manifest schema validity (handled by the AEOS validator)
- Templating syntax parses against the declared `template_dialect`
- All `{{ arguments.<x> }}` references resolve to declared
  `arguments[*]` entries
- All `{{ context.* }}` references resolve to the platform-context
  schema
- Body length under a per-tier ceiling (default: 4 KB at `core`,
  8 KB at `advanced`; a runaway prompt is a real risk)
- Forbidden patterns: claims to invoke verbs the extension doesn't
  ship, contradicts the platform's `axi-cli-gravity` rule, etc.
- Required preamble (the contribution must include the provenance
  marker; missing it is an error)
- Fill-point compatibility (the named fill point exists on the
  parent prompt; the parent prompt itself exists)

`axi ext lint --strict` makes lint warnings errors. New extensions
that contribute prompts must pass `--strict` before publish.

### 4.9 Capability projection (normative)

A registered **capability** — a skill-function (`SkillSpec`, ADR-056),
the executable unit `(params, ctx) -> SkillResult` with a typed `inputs`
schema — is the **single source of truth** for what the platform can do
(ADR-063). Its CLI verb (§4.3), MCP tool (§4.2), generated SKILL.md
(§4.6), and agent-facing LLM function-tool are **projections** of that
one capability. Projections are generated by the shared projector
`axiom.infra.capability_projection`. **No surface and no extension
defines its own name-mangling or schema translation** (ADR-072).

This section is normative for every extension. Where §4.2 (tool) and
§4.6 (skill) describe a surface, they describe a *projection* governed
by the rules here.

#### 4.9.1 One naming convention

A capability name is dotted: `<namespace>.<verb>` (the CLI noun+verb of
§4.3). Surface-safe names — where a transport forbids `.` (MCP tool
names, LLM function names) — are derived by the single shared round-trip:

- `capability_to_surface_name("press.draft") == "press__draft"`
- `surface_to_capability_name("press__draft") == "press.draft"`

MCP tool names and agent-tool names are **derived** from the capability
name; manifests MUST NOT restate them. The separator is `__` (double
underscore); it is reserved in capability namespaces and verbs.

#### 4.9.2 One schema derivation

The input JSON Schema is derived once, from the capability's typed
`inputs`, by the shared projector. Every surface consumes that schema;
none re-authors it. Hand-crafted per-surface schemas are non-conformant.

#### 4.9.3 Side-effects and approval live on the capability

A capability declares `side_effects` and `idempotent` (already required
of `tool` in §4.2). The READ/WRITE → approval-gating decision is read
from that declaration and honored identically by every surface — the CLI
confirm prompt, the MCP `side_effects` annotation, and the chat approval
gate. A surface MUST NOT invent its own default (e.g. "treat every skill
as WRITE"); the policy is declared once, on the capability.

#### 4.9.4 Surface and agent exposure is declared and scoped

Each `[[extension.provides]]` capability block declares the surfaces it
projects onto and the agents permitted to call it:

```toml
[[extension.provides]]
kind = "skill"                       # the executable capability (ADR-056)
name = "press.draft"
entry = "axiom.extensions.builtins.publishing.skills:draft"
path = "skills/draft/SKILL.md"
side_effects = false                 # READ → auto-approve; true → confirm-gated
idempotent = true
surfaces = ["cli", "mcp", "agent_tool"]   # which projections to emit
exposed_to_agents = ["press", "axi"]      # scope for the agent_tool projection
```

> **Schema note (0.2 draft).** Earlier text showed `side_effects`, `idempotent` and
> `surfaces` on the skill table while the manifest schema rejected them. The schema
> now accepts them on `kind = "skill"`, together with the chaining declarations of
> §4.11.3, so this example is valid as written.

**An agent's LLM tool list is the scoped projection of the capabilities
it is authorized to call — never "all skills."** Discovery is dynamic
(the projector walks the `SkillRegistry`); exposure is bounded by
`surfaces` + `exposed_to_agents`. This is the structural guard against
tool-explosion: an agent is handed a small, declared toolset, not the
entire platform surface.

#### 4.9.5 Terminology (normative)

Two distinct concepts MUST NOT be conflated:

- **Capability / skill-function** — the executable unit registered in
  `SkillRegistry` (ADR-056). This is what projects onto CLI/MCP/agent-tool.
- **SKILL.md** — the agentskills.io instruction document (§4.6),
  *generated from* the capability (ADR-063). It is documentation for an
  LLM, not the invocation contract.

The `kind = "skill"` manifest block declares the capability; its `path`
points at the generated SKILL.md.

#### 4.9.7 Chaining declarations (normative; ADR-185)

This subsection fixes the runtime semantics. Where the declarations live, the
levels, the lint and the wire mapping are in §4.11.

A capability MAY declare how it relates to the capabilities around it, in data.
It never imports another capability to do so. All fields are optional on
`SkillSpec`; `None` means "has not said" and an empty tuple means "says there is
nothing". An undeclared capability runs exactly as before.

| Field | Type | Meaning |
|---|---|---|
| `risk` | `read` \| `reversible-write` \| `destructive` | How much harm a mistaken call can do. Same vocabulary as the chat command registry. |
| `requires` | `tuple[Precondition]` | `Precondition(id, description, check=None, fix, consumes="", param="")`. `check(params, ctx)` is cheap and side-effect free, evaluated at call time; a check that raises is unmet. `fix` is the exact command or setting and may name `{param}`s. `consumes` names a `Ref` kind the caller must supply as `param`. |
| `produces` | `tuple[Ref]` | `Ref(kind, how_to_use, field="")`: a typed reference the result's `value` carries (`plan_hash`, `snapshot_id`, `session_id`, `fragment_id`). |
| `next_steps` | `tuple[NextStep]` | `NextStep(target, when, why, feeds={})`. `target` is a registered capability or a command line; `when` is `ok`, `error`, `always` or `produced:<kind>`; `feeds` maps the target's input to a produced kind. |
| `recipes` | `tuple[str]` | Recipes this capability is a step of. Must match the recipe catalog in both directions. |

**Runtime (at the dispatch chokepoint).** (a) Unmet `requires` return
`ok=false` with `chain.unmet = [{id, why, fix}]`; the body does not run.
(b) A successful or failed call adds the versioned key
`chain = {v: 1, next: [...], produced: [...], chain_id?, step_id?, parent_step?}`
to the result envelope, only when it has something to say; `next` lists declared
follow-ups that exist and are exposed on the calling surface, plus any the
capability returned. (c) A caller-supplied reserved argument
`_chain = {chain_id, parent_step}` is stripped before parameter validation,
given a minted `step_id`, and written into the action audit record. (d) A
`destructive` capability reached from a surface other than the CLI and the
runner requires `plan_hash`: the first call opens a plan on the approval queue
and returns the hash, and the capability runs only with the hash of an approved,
unspent plan for exactly those parameters. (e) A failed call, including every
refusal in (a) and (d), is returned over MCP as a tool execution error
(`isError: true`), never as a protocol error, with the body unchanged.

**Recipes** are data (`axiom.infra.recipes.Recipe`): intent, ordered steps, input
wiring by ref (`$param`, `$step.kind`), approval on destructive steps, rollback.
They are validated by `recipe.check` and dry-run by `recipe.plan`, and published
as MCP prompts `axiom_recipe__<id>` and as capabilities-block lines. See
`spec-builtin-mcp-server.md` §6.6.

#### 4.9.6 Lint expectations

`axi ext lint` asserts single-source-of-truth, mirroring ADR-063's
`--check`:

- Every checked-in surface artifact (SKILL.md, MCP tool name + schema)
  is byte-identical to the projector's output for that capability.
- No `[[provides]]` block restates a name or schema the projector
  derives.
- `surfaces` values are a subset of `{cli, mcp, agent_tool, skill_md}`;
  `exposed_to_agents` references declared agents.
- A capability projecting to `agent_tool` declares `side_effects`.
- Chaining declarations are verified against the **chain level the extension
  claims** (§4.11): at Level B every capability declares `risk`, every one that is
  not `read` declares `requires` (or an explicit empty list), every `requires`
  entry carries a `fix`, every `next_steps` target resolves and every produced ref
  is consumed or documented; at Level C recipes validate against the catalog. An
  extension that claims nothing is Level A and is not asked for more. This
  repository additionally keeps its own ratchet (a debt list that only shrinks,
  expressed per extension as the level each built-in claims;
  `tests/infra/test_chain_level_claims_ratchet.py`).

`axi ext lint --strict` makes these errors.

---

### 4.10 api

An HTTP router a verb contributes to the **composed `/api/v1` surface**, so a
client sees one base URL, one auth posture and one versioning story regardless
of which verbs are installed.

The platform's mount registry refuses conflicting prefixes, so verbs cannot each
mount `/api/v1/<verb>` beside the one the API shell already holds; and the shell
importing every verb would invert the dependency (installing one verb would drag
in the rest). So a verb *declares* its subpath and the shell discovers it,
resolving the entry lazily at compose time.

- Provides: a `register_routes(router, *, subpath=...)` callable
- Declares: `subpath` (must start with `/`), `entry`, optional `description`,
  `deployment_profile`, `requires_authz`
- Rules: subpaths MUST NOT collide; a contribution that fails to import costs
  its own verb and is logged at ERROR, never the whole surface; discovery is
  ordered deterministically (by subpath) so the composed app is identical on
  every machine.

Distinct from `service` (§4.4), which mounts its own prefix on the serving
substrate and returns a `MountSpec`. An `api` contribution joins an existing
surface instead of claiming a new one.

> Numbered 4.10 rather than slotted beside the other capability kinds on
> purpose: accepted ADRs (072, 073, 074) cite "AEOS §4.9" for capability
> projection, and those documents are immutable. Renumbering the section they
> point at would silently redirect every one of those references to a different
> subject; an out-of-sequence number is the cheaper cost.


### 4.11 Chain readiness (normative; AEOS 0.2 draft)

This section turns the chaining declarations of §4.9.7 (ADR-185) from a repository
guard into part of the standard: a vocabulary, a place to declare it, three levels
an extension can claim, and a lint that verifies the claim. It is **additive**.
An extension that declares nothing keeps conforming, at Level A, unchanged.

Items marked **AEOS delta** have no public counterpart as of 2026-10-08 (see
`docs/reference/aeos-chaining-prior-art-2026-10.md`); the rest maps onto an
existing public shape and uses it (§4.11.4, §4.11.5).

#### 4.11.1 Version story

This is an additive minor: **AEOS 0.2 (draft)**. It is carried inside this
document, as §4.11 and §4.12 plus the changes named in the revision history, until
0.2 is ratified; no new file is cut for a draft. The rules:

- `aeos_version` keeps its meaning (the spec version the extension conforms to).
  Every existing manifest says `0.1.0` and stays valid with no edit. The schema is
  extended in place; no 0.1 manifest becomes invalid.
- A manifest that claims chain level B or C MUST declare `aeos_version >= "0.2"`
  (rule AEOS101). The claim and the version it needs are checked together.
- The chain level is a **separate axis** from Bronze, Silver and Gold (§12). Those
  grade packaging, signing and federation readiness; this grades how well an
  extension's skills describe their relation to each other. An extension has both,
  independently: "Silver, chain level B".
- `[extension] chain_level = "A" | "B" | "C"`; absent means `A`.

#### 4.11.2 Levels

| Level | Name | An extension at this level |
|---|---|---|
| **A** | baseline | Declares nothing it does not want to. Any declaration it does make is well formed and does not contradict itself (AEOS112, AEOS113, AEOS119). |
| **B** | chain-ready | Every skill declares `risk`. Every skill that is not a read declares `requires` (an explicit empty list is an answer: nothing must hold first). Every `requires` entry carries an exact `fix`. Every produced ref is consumed by something or says what to do with it. Every `next_steps` target resolves, and every `feeds` / `produced:` kind is one the skill produces. Every `recipes` name is a recipe that exists. |
| **C** | recipe-publishing | Level B, and the extension ships at least one recipe, each recipe valid against the skill catalog (every step real, every `$step.kind` wired from an earlier step that produces it, every destructive step carrying an approval point), with recipe membership declared on both sides. |

C implies B implies A. A claim that the declarations do not support fails the lint
as AEOS102; a declaration set that supports more than the claim is noted as AEOS103
and is not an error. `axi ext lint --chain-level B` tries an extension against a
level it has not claimed and lists, per skill, each finding with its exact fix; it
is how an author moves up one level at a time.

The levels are ordered by cost to the author, not by importance. Level B is what a
caller needs in order to avoid guessing (what a refused call needs, what is safe to
repeat, where a person must say yes). Level C is what a caller needs in order to do
a whole task without improvising the order.

#### 4.11.3 Where declarations live

**Decision: the manifest `kind = "skill"` table carries the declarations, as data;
the `SkillSpec` in code stays the source of truth and the table is its generated
projection.** This extends §4.9.4, whose example already showed `side_effects` and
`surfaces` on the table although the schema rejected them; the schema now accepts
them and the new fields (`risk`, `idempotent`, `open_world`, `requires`,
`produces`, `next_steps`, `recipes`).

```toml
[[extension.provides]]
kind = "skill"
name = "ws.make"
entry = "ws.skills.impl:make"
path = "ws/skills/make"
risk = "reversible-write"            # read | reversible-write | destructive
side_effects = true
idempotent = false
requires = [{ id = "named", description = "a name is given", fix = "pass name=<text>" }]
produces = [{ kind = "thing_id", how_to_use = "pass as thing to ws.drop" }]
next_steps = [{ target = "ws.drop", when = "produced:thing_id", why = "remove it again", feeds = { thing = "thing_id" } }]
recipes = ["tidy"]
```

Why the manifest and not a sidecar: `axi ext lint` is static by rule (it reads the
manifest and never imports extension code, because the extension may be untrusted
and may not even be installed), and an MCP-facing or registry consumer needs the
same facts without loading the extension. A `check` callable is code and cannot be
in a manifest, so the table carries the data half only: `id`, `description`, `fix`,
and the `consumes` / `param` wiring. Absent means "has not said"; an empty list
means "says there is nothing", and Level B reads the difference.

Why the code stays the source of truth: ADR-063 already makes the registered
capability the single source for every projection, and `axi skills emit` already
generates these tables from it. The generator now writes the declarations too, so
the manifest cannot be hand-edited into disagreement without the lint noticing: for
a built-in extension, whose skills the host can bind, rule AEOS130 compares the
table with the code and fails on any difference. A separate generated sidecar file
would add a second artifact to keep in sync and a second thing for a consumer to
find; the manifest already is the place a consumer looks. How an MCP-facing
consumer reads them without importing extension code: over the wire from
`tools/list` (§4.11.4), or offline from the manifest table (this section).

#### 4.11.4 MCP mapping (public-standard alignment)

The MCP skill-tool projection emits the following, so a client that has never heard
of AEOS still gets the safety hints and ignores the rest. Field names are those of
the MCP `ToolAnnotations` type (verified against the installed `mcp` library).

| AEOS | MCP | Rule |
|---|---|---|
| `risk = "read"` | `readOnlyHint: true`, `destructiveHint: false` | |
| `risk = "reversible-write"` | `readOnlyHint: false`, `destructiveHint: false` | |
| `risk = "destructive"` | `readOnlyHint: false`, `destructiveHint: true` | |
| `idempotent` | `idempotentHint` | |
| `open_world` | `openWorldHint` | |
| `risk` undeclared | `readOnlyHint` from `side_effects`; `destructiveHint` **unset** | "Has not said" is never projected as harmless. |
| `produces` | `outputSchema` (each ref a property of `value`, tagged `x-aeos-ref: <kind>`) and `structuredContent` on the result | |
| unmet `requires` | the call result has `isError: true` (a tool execution error, not a protocol error) and `structuredContent.chain.unmet[]` | |
| `requires`, `produces`, `next_steps`, `recipes`, `risk` | `_meta["ai.axiom/chain"] = {v: 1, ...}` on the tool | |
| `chain_id`, `step_id` | result `_meta["ai.axiom/chain"]`, mirrored from the body | `chain_id` is **not** a W3C `traceparent`: a trace id is transport-scoped, a chain id is the durable audit key. Both may be present. |

The `_meta` key is reverse-DNS prefixed (`ai.axiom/`), following the MCP rule that
vendor keys carry a reverse-DNS prefix and that un-prefixed keys are reserved. A
tool that declares nothing has no `_meta` and no `outputSchema`, byte-identical to
before. The hints are advisory and untrusted by MCP's own terms; the enforcement is
server-side (§4.11.7), and a client that ignores every hint is still refused where a
requirement is unmet.

Recipes are published as MCP prompts named `axiom_recipe__<id>` (§4.12). Where a
client supports Multi Round-Trip Requests, the approval of a destructive plan MAY
be carried as an `InputRequiredResult` with the plan hash held in integrity-protected
`requestState` (HMAC or AEAD, bound to the principal, short-lived, bound to a digest
of the originating request); single use remains enforced by the server, which MRTR
leaves to it. The explicit two-call `plan_hash` form (§4.11.7) is the fallback and
the form every conformant server implements.

#### 4.11.5 Follow-ups and pre-filled arguments

A `next_steps` entry is expressed in OpenAPI Link Object vocabulary so that a reader
of OpenAPI Links or Arazzo recognizes it:

- `operationId`: the target tool name (or `command` for a non-tool follow-up).
- `parameters`: the target's input mapped to a runtime expression over this tool's
  structured result, `$response.body#/value/<ref>`, where the part after `#` is an
  RFC 6901 JSON Pointer.
- `description`: the reason (`why`).
- `when`: `ok`, `error`, `always` or `produced:<kind>`. OpenAPI Links have no
  condition; this is an AEOS addition.

In a live result the same entry also carries `with`, the argument already resolved
from this result's produced refs, so a caller does not have to evaluate a pointer.

#### 4.11.6 Recipes and the Arazzo projection

A recipe is projected into an Arazzo 1.1 workflow by `axiom.infra.recipes.to_arazzo`:
`workflowId`, `summary`, `inputs` (a JSON Schema of the recipe parameters), and per
step `stepId`, `operationId` (the tool name), `parameters` with `$inputs.<param>` and
`$steps.<id>.outputs.<kind>` expressions, `dependsOn` and `outputs`. Everything
Arazzo has no word for rides in `x-aeos-*` extension fields (`x-aeos-approval`,
`x-aeos-undo`, `x-aeos-rollback`), which Arazzo permits. The recipe is the authority
and the projection is regenerated from it; the projection function exists now and
nothing consumes it yet.

#### 4.11.7 The AEOS delta: contracts with rule ids

These five contracts are the part of the chaining layer with no public counterpart.
Each has normative text, a rule id, and a check that can fail. Rules AEOS110 to
AEOS130 are static (`axi ext lint`); rules AEOS140 to AEOS150 are behavioural and are
checked by `axiom.infra.chain_conformance`, which runs a real skill through the real
dispatch chokepoint and returns violations.

1. **Call-time `requires` with a mandatory exact `fix` (AEOS113, AEOS141).** A
   precondition is evaluated at call time, before the effect. Every `requires` entry
   MUST carry a non-empty `fix`: the exact command or setting that satisfies it
   (schema-enforced, and AEOS113 when the schema was not run). An unmet requirement
   MUST have no effect, MUST return `ok=false` with `chain.unmet = [{id, why, fix}]`,
   and following the `fix` literally MUST clear it. A refusal is never a dead end.
2. **Result-embedded `next_steps` with pre-filled arguments (AEOS116, AEOS142).** A
   follow-up that declares `feeds` MUST arrive with the argument already filled from
   the result's produced ref, only when that ref was actually produced, and only for
   a target that exists and is exposed on the calling surface.
3. **Recipe validation against the live catalog (AEOS120 to AEOS127).** A recipe is
   rejected at lint time if a step names a skill that is not real, an input is wired
   from a step that does not produce that kind, a `requires` consumed ref has no
   source, a destructive step lacks an approval point (AEOS127: mandatory, not
   advisory), or membership is declared on only one side (AEOS124).
4. **Plan/apply as a declared contract (AEOS140).** A `risk = "destructive"` skill
   is a plan/apply pair by declaration. From a surface where a person is not already
   at the verb (MCP, chat, browser), the first call MUST do nothing and return a
   `plan_hash` and the held action; a hash with no approval behind it MUST do
   nothing; an approved hash applied to different parameters MUST do nothing; an
   approved plan MUST apply exactly once; the approval MUST be spent by that call,
   even when the body then fails. The hash is a digest of the capability and its
   parameters. The CLI and the runner are exempt because the person, or the approval
   queue itself, is already the gate.
5. **Chain identity in a tamper-evident audit (AEOS150).** A caller-supplied
   `_chain = {chain_id, parent_step}` MUST be stripped before parameter validation,
   given a minted `step_id`, and written into the action audit record; the audit
   MUST still verify, and altering a recorded chain id MUST be detected.

#### 4.11.8 Rule catalogue

Static rules run in `axi ext lint`; `--chain-level` tries a level that is not
claimed. Findings carry the rule id, the skill, and the exact fix.

| Id | Binds at | Severity | Rule |
|---|---|---|---|
| AEOS100 | A | error | `chain_level` is not one of A, B, C |
| AEOS101 | A | error | a claim above A needs `aeos_version >= 0.2` |
| AEOS102 | A | error | the claimed level is not achieved |
| AEOS103 | note | info | a higher level is achieved than claimed |
| AEOS104 | note | info | a Level A extension has skills that would need declarations for B |
| AEOS110 | B | error | a skill declares no `risk` |
| AEOS111 | B | error | a skill that is not a read says nothing about `requires` |
| AEOS112 | A | error | `risk` contradicts `side_effects` |
| AEOS113 | A | error | a `requires` entry has no `fix` |
| AEOS114 | B | error | a produced ref is consumed by nothing and says nothing about use |
| AEOS115 | B | error | a `next_steps` target does not resolve |
| AEOS116 | B | error | a `next_steps` entry feeds or waits on a ref the skill does not produce |
| AEOS118 | B | error | `recipes` names a recipe that exists nowhere |
| AEOS119 | A | error | a chaining declaration is malformed |
| AEOS120 | C | error | no recipe is shipped |
| AEOS121 | C | error | a recipe file is missing or unparseable |
| AEOS122 | C | error | a recipe is malformed |
| AEOS123 | C | error | a recipe does not validate against the skill catalog |
| AEOS124 | C | error | recipe membership is not declared in both directions |
| AEOS125 | note | warning | a reference to another extension cannot be verified statically |
| AEOS127 | C | error | a destructive recipe step has no approval point |
| AEOS130 | A | error | the manifest copy of a declaration differs from the code |
| AEOS140 | runtime | error | plan/apply contract |
| AEOS141 | runtime | error | requires contract |
| AEOS142 | runtime | error | next_steps contract |
| AEOS150 | runtime | error | audit contract |

A reference to a skill of another extension is verified against the host's registry
when lint runs inside an Axiom install, and is AEOS125 (a warning) when it cannot be.

#### 4.11.9 Public alignment and candidate upstream proposals (non-normative)

Decision pending, not authorised. This subsection records what maps to a public
shape and which parts of the AEOS delta could later be put to a public body. It does
not start that work, and per ADR-032 AEOS is not advertised externally.

Maps to an existing public shape: the annotation hints (§4.11.4); typed outputs via
`outputSchema` and `structuredContent`; follow-up wiring via OpenAPI Link Object
fields and RFC 6901; recipes via MCP prompts and an Arazzo-compatible step graph;
approval transport via MRTR `requestState`; tool errors via `isError`.

Candidates for a later upstream proposal, each only after a decision: call-time
`requires` with a mandatory `fix` (the MCP proposal SEP-1385 for a `requires` field
was closed as dormant for lack of a sponsor, and had no remediation field); a graded
three-value risk (SEP-2793 on graded risk metadata is stalled); result-embedded
`next_steps` (SEP-1610 on tool chaining, a model-side design, was also closed for
lack of a sponsor); recipe validation rules; the plan/apply pair as a declared
contract; a chain id in audit. A proposal that needs a named sponsor and a working
group home would end as those did without one.

### 4.12 recipe (runtime-consumed kind)

A `kind = "recipe"` block declares a **recipe**: a vetted chain of skills shipped as
a data file (§4.11.6, `axiom.infra.recipes.Recipe`).

```toml
[[extension.provides]]
kind = "recipe"
name = "tidy"                    # lowercase kebab, equals the id in the file
path = "recipes/tidy.toml"
description = "Make a thing and remove it."
```

**A recipe is not a ninth core capability kind, and it is not folded into `skill` or
`prompt`.** It is not a skill: it has no body and nothing runs it (there is no runner;
the agent follows it, calling each skill through the ordinary chokepoint so every step
keeps its own authorization, audit and refusal). It is not a prompt: a prompt is a
template of text, while a recipe is data that is validated against the skill catalog,
and only its *publication* is a prompt (`axiom_recipe__<id>`). Making it a ninth core
kind would break the rule that the core list is closed within a version, which §4 had
to correct twice already. So it joins `signal_type`, `safety_check`, `api` and
`channel_adapter` as a runtime-consumed kind: declared in the schema, consumed by a
runtime, outside the count of eight.

The recipe file (TOML): `id`, `intent`, `rollback`, optional `[params]`, and `[[step]]`
tables (`id`, `skill`, `purpose`, `approval`, `undo`, `[step.inputs]`). Input values are
a literal, `$param`, or `$step.kind`. The manifest side exists so that lint can see an
extension's recipes without importing it.


## 5. Directory Layout

### 5.1 Canonical compound layout

```
<extension-name>/                       # purpose-named directory (e.g., classroom, connect)
├── <extension-package>/                # Python package (matches directory name)
│   ├── __init__.py                     # PUBLIC API surface — import-linter enforced
│   ├── agents/                         # optional — contains agent modules
│   │   └── <agent-name>/
│   │       ├── __init__.py
│   │       ├── agent.py
│   │       └── persona.md              # agent's own system prompt / role definition
│   │                                   # NOT a standalone skill; internal to the agent
│   ├── tools/                          # optional — contains tool modules
│   │   └── <tool-name>/
│   ├── commands/                       # optional — contains cmd modules
│   │   └── <noun>/
│   ├── services/                       # optional — contains service modules
│   │   └── <service-name>/
│   ├── adapters/                       # optional — contains adapter modules
│   │   └── <integration-name>/
│   ├── skills/                         # optional — contains STANDALONE reusable skills
│   │   └── <skill-name>/               # not tied to any agent; any agent with access may invoke
│   │       ├── SKILL.md                # agentskills.io format
│   │       ├── references/
│   │       └── scripts/
│   ├── hooks/                          # optional — contains hook modules
│   │   └── <hook-name>.py
│   ├── signals.py                      # signal type registrations (if any)
│   ├── templates.py                    # notification templates (if any)
│   ├── _internal/                      # strictly private, never imported externally
│   │   └── ...
│   └── py.typed                        # PEP 561 type-information marker
├── tests/
│   ├── unit_tests/
│   │   ├── test_standard.py            # inherits from axiom_tests.unit_tests
│   │   └── test_<specific>.py
│   ├── integration_tests/
│   │   └── test_standard.py            # inherits from axiom_tests.integration_tests
│   ├── fixtures/
│   └── conftest.py
├── docs/
│   ├── prds/
│   │   └── prd.md                      # product requirements for this extension
│   ├── specs/
│   │   └── spec.md                     # technical specification
│   ├── decisions/
│   │   └── adr-001-<title>.md          # extension-level ADRs
│   ├── working/                        # in-flight design docs
│   ├── overview.md                     # user-facing overview
│   └── reference/                      # API reference, typically generated
├── AGENTS.md                           # coding-agent guidance (or symlink to docs/AGENTS.md)
├── README.md                           # user-facing landing
├── CHANGELOG.md                        # Keep-a-Changelog format
├── LICENSE                             # license file
├── pyproject.toml                      # independently pip-installable
├── axiom-extension.toml                # AEOS manifest
└── .importlinter                       # local import-linter config (optional; root may suffice)
```

### 5.2 Required files

Every **standalone** AEOS extension (pip-installable) MUST have:
- `pyproject.toml` (or language-equivalent build manifest)
- `axiom-extension.toml` (AEOS manifest)
- `README.md` (user-facing)
- `CHANGELOG.md` (Keep-a-Changelog)
- `LICENSE`
- `<package>/__init__.py` with `__all__` declared
- `tests/unit_tests/test_standard.py` inheriting from an `axiom_tests` base class

**Built-in extensions** (those that ship inside a host package, e.g. Axiom's `extensions/builtins/`) relax to:
- `axiom-extension.toml` (AEOS manifest, with `builtin = true`)
- `<ext>/__init__.py` — flat layout; `__all__` optional (the host package owns the public surface)
- `tests/unit_tests/test_standard.py` inheriting from an `axiom_tests` base class

For built-ins, `README.md`, `CHANGELOG.md`, `LICENSE`, and `pyproject.toml` belong to the host package rather than the individual extension. When the built-in extracts to a standalone repository, those files get added as part of the extraction.

### 5.1.1 Flat vs. compound layout

The canonical compound layout in §5.1 applies to **standalone** extensions. For **built-in** extensions (manifest sets `builtin = true`), the extension's root directory IS the Python package — no inner `<ext>/<ext>/` nesting:

```
<host-package>/extensions/builtins/<ext>/
├── __init__.py                            # flat: the ext IS the package
├── axiom-extension.toml                   # with builtin = true
├── agents/<name>/persona.md               # capability subdirs flat under <ext>/
├── tools/…
├── commands/…
├── tests/
│   └── unit_tests/test_standard.py
└── docs/…
```

Imports use the host package's dotted path directly: `host.extensions.builtins.<ext>.module:symbol`. The flat layout keeps built-in import paths legible and reflects that built-ins share their host's distribution metadata. Extraction to a standalone repository moves the flat directory to its own repo and wraps it with the compound layout's pyproject / README / CHANGELOG / LICENSE.

### 5.3 Optional files

- `AGENTS.md` — strongly recommended for any extension with non-trivial source.
  Per §2, this is the only hand-authored context file; harness-specific
  context files are symlinks to it or generated from it, never authored
- `docs/` — strongly recommended; required for any extension with ≥2 capability kinds
- Capability-kind subdirectories: required for provided kinds, absent otherwise — an empty kind directory is scaffold residue to delete, not conformance (AEOS040 checks provided kinds only)
- `.importlinter` — if local rules beyond the root configuration are needed
- `conftest.py` at various levels

### 5.4 Package naming

The directory name and Python package name are identical and are the extension's purpose name. No type suffix. Valid examples: `classroom`, `connect`, `memory`, `syllabus_extraction`, `process_monitoring`. Invalid: `classroom_domain`, `memory`, `connect_adapter`.

Legacy extensions using the `_agent` suffix may retain it during a deprecation window but should migrate by AEOS 1.0.

---

## 6. Manifest Format

The `axiom-extension.toml` file is the AEOS manifest. It is TOML v1.0.0 with a defined schema.

### 6.1 Schema overview

```toml
# AEOS Manifest — axiom-extension.toml

# ---- Extension identity ----
[extension]
name = "classroom"                      # must match directory and package name
version = "0.1.0"                       # semver
description = "Classroom learning management, analytics, and research"
owner = "ut-austin"                     # or "b-tree-labs", "b-tree-ventures", etc.
license = "Apache-2.0"
homepage = "https://keplo.dev"          # optional
repository = "https://github.com/ut-austin-ne/keplo"
aeos_version = "0.1.0"                  # AEOS spec version this extension conforms to
chain_level = "A"                       # optional (0.2 draft, §4.11): A (default), B or C; B and C need aeos_version >= 0.2
classification_ceiling = "public"       # maximum classification this extension handles
trust_profile = "standard"              # required trust profile

# ---- Compatibility with other standards ----
[extension.compatibility]
mcp = ">= 2025-11"                      # MCP version supported
a2a = ">= 0.3"                          # A2A protocol version
python = ">= 3.11"                      # minimum Python
axiom = ">= 0.14, < 0.17"               # Axiom version constraint
platforms = ["linux", "darwin", "windows"]

# ---- Provided capabilities ----
# Each [[extension.provides]] block declares one capability. Multiple allowed.

[[extension.provides]]
kind = "agent"
name = "chalke"
entry = "classroom.agents.chalke:ChalkeAgent"
persona = "classroom/agents/chalke/persona.md"   # agent's own system prompt
description = "Classroom instructor companion agent"
requires_signals = ["student_absence", "help_request", "engagement_drop"]
uses_skills = ["subject_tutor", "debug_assignment"]   # may invoke these standalone skills

[[extension.provides]]
kind = "tool"
name = "syllabus_extraction"
entry = "classroom.tools.syllabus_extraction:SyllabusExtractor"
description = "Extract course structure from uploaded syllabus"
idempotent = true
side_effects = "none"

[[extension.provides]]
kind = "cmd"
noun = "enrollment"
entry = "classroom.commands.enrollment:cli"
description = "Manage student enrollment"
subcommands = ["add", "remove", "list", "notify"]

[[extension.provides]]
kind = "adapter"
integration = "canvas_lms"
entry = "classroom.adapters.canvas:CanvasAdapter"
auth_methods = ["oauth2", "api_token"]
capabilities = ["grade_push", "roster_sync", "assignment_create"]

[[extension.provides]]
kind = "skill"
name = "subject_tutor"
path = "classroom/skills/subject_tutor/"
description = "Standalone skill for tutoring a course subject; invokable by any agent with access"

[[extension.provides]]
kind = "hook"
events = ["session.started", "session.ended"]
entry = "classroom.hooks:session_hooks"
priority = 100
fail_mode = "warn"

[[extension.provides]]
kind = "signal_type"
names = ["student_absence", "help_request", "quiz_submitted", "session_ended", "engagement_drop"]
entry = "classroom.signals"

# ---- Consumed capabilities (dependencies) ----
[[extension.consumes]]
kind = "core"                           # axiom-core public API
package = "axiom"
version = ">= 0.14, < 0.17"

[[extension.consumes]]
kind = "extension"                      # another AEOS extension
package = "vega-trust"
version = ">= 0.1, < 0.2"
capabilities = ["federation", "trust_profile"]

# ---- Federation characteristics ----
[extension.federation]
shareable = true                        # can be distributed via federation manifest channel
requires_attestation = true             # requires behavioral attestation
quarantine_recoverable = true           # supports quarantine/recovery ceremony

# ---- Signing and provenance ----
[extension.signing]
required = true                         # extension MUST be signed to be published
methods = ["sigstore"]                  # signing methods accepted
publisher_identity = "ut-austin"        # expected signer

# ---- Testing conformance ----
[extension.testing]
standard_tests = ["unit", "integration"]
test_base_class = "axiom_tests.standard.ExtensionStandardTests"
minimum_coverage = 80
```

### 6.2 Required manifest fields

`[extension]` section requires: `name`, `version`, `description`, `license`, `aeos_version`.

Every extension MUST declare at least one `[[extension.provides]]` block.

### 6.2.1 The `builtin` field

`[extension].builtin` is an optional boolean (default `false`) that signals an extension ships inside a host package — typically Axiom's `extensions/builtins/` — rather than as a standalone pip-installable distribution. When `builtin = true`:

- Layout switches to flat per §5.1.1 (`<ext>/__init__.py` instead of `<ext>/<ext>/__init__.py`).
- Required-files set relaxes per §5.2 (only `axiom-extension.toml` and `tests/unit_tests/test_standard.py` are required at the extension root; README, CHANGELOG, LICENSE, pyproject belong to the host package).
- Conformance tools (`axi ext lint`, `axi ext scan`, `ExtensionStandardTests`) apply the relaxed checks.

External, pip-installable extensions omit `builtin` (or set it to `false`) and follow the full compound layout + required-files set.

### 6.2.2 `[agent.sandbox]` — what the host will let an agent do

An AEOS deployment is a host plus extensions the host's author did not
write. The host therefore cannot sandbox agents by enumerating permitted
behaviours; it sets a **floor** and reads a **declaration**. An extension
author needs three facts, and the full contract is in
[agent-sandboxing.md](../security/agent-sandboxing.md).

**1. There is a floor you cannot lower.** A platform-managed agent runs as
a user-level service, never root. On Linux the host applies
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `RestrictNamespaces`, `LockPersonality`,
`MemoryDenyWriteExecute`, and confines writes to the agent's own state
directory. None of it is reachable from a manifest or at runtime. On macOS
there is no confinement today — write your agent as though there were, and
do not rely on the host to stop you.

**2. Three axes you may declare.** In the manifest, so an operator can read
them before installing:

```toml
[agent.sandbox]
protect_home = "tmpfs"               # relax: default "read-only", also "off"
read_write_paths = ["~/.config/gh"]  # relax: ADDED to your state dir, never instead of it
network = false                      # tighten: default true
```

`network = false` is the only axis that gives something up, and an agent
that reads credential material, logs or user documents should take it: the
floor bounds WRITES, so a reading agent's risk is egress, not corruption.

**3. An over-broad declaration is refused, not trimmed.** A
`read_write_paths` entry wide enough to defeat `ProtectSystem` fails when
the unit is generated. An unknown `protect_home` value fails the same way.
The platform does not apply the default while your manifest claims
otherwise, because that leaves an operator auditing a statement that was
never true.

**What this means for your design.** Declare the narrowest thing that
works, and expect to justify a relaxation to whoever installs you — each
one is logged at install and shown in `axi agents status`. If your agent
needs something the floor denies, that is a conversation with the host,
not a runtime workaround: there is no path that widens the sandbox after
install.

A claim of chain level B or C (§4.11) additionally requires `aeos_version` of `0.2` or
later; the validator accepts any `major.minor[.patch]` and lint enforces the pairing
(AEOS101).

### 6.3 Manifest validation

`axi ext lint` validates the manifest against the AEOS JSON Schema (published at `spec.axiom-os.ai/aeos/0.1/schema.json` when AEOS is made public, or at `docs/specs/aeos-schema-0.1.json` in the Axiom repo today).

### 6.4 Root schema is strict

The AEOS 0.1 schema's root object sets `additionalProperties: false`. The `Extension` table is strict, and every root-level section the runtime still consumes is enumerated in the schema:

| Root section | Schema definition | Status |
|---|---|---|
| `[extension]` | `Extension` | Strict — every key must be an AEOS-defined property |
| `[agent]` | `AgentLifecycle` | Permissive — pre-AEOS daemon-lifecycle block. Future migration to a `kind = "service"` provides block is tracked in the parity-gaps doc. |
| `[[connections]]` | `Connection` | Permissive — pre-AEOS integration block. Future migration to `kind = "adapter"` is tracked. |
| `[chat_tools]` | `ChatToolsModule` | Permissive — module-level tool registry. Future migration to per-tool `kind = "tool"` blocks is tracked. |
| `[skills]` | `SkillsConfig` | Configures the directory the skills scanner walks. |
| `[[providers]]` / `[[extractors]]` / `[mcp_servers]` / `[[prompt_contributions]]` | per-section permissive | Runtime-consumed; schemas accommodate the existing shapes. |
| `[defaults]` | `ExtensionDefaults` | Permissive — extension-private deployment default values the extension's own code reads back from its manifest (e.g. `llm_serving` `load_config()`). |
| `[[flags]]` | `FlagDeclaration` | Strict — feature flags this extension declares: `key`, `description`, `default`, `default_by_env`, `protected_in` (ADR-210, `spec-flags.md`). |
| `[firing]` | `FiringConfig` | Permissive — PULSE firing-mode declaration (`schedule` extension); declared ahead of the PULSE-2 consumer. |

Beyond the eight core capability kinds, the schema also enumerates the
runtime-consumed `signal_type`, `safety_check` and `recipe` (§4.12) kinds, the `api` kind (a
declared slice of the composed `/api/v1` surface, discovered by the webapp
extension via `ApiSurfaceDef`), and the `channel_adapter` kind (per the
notifications extension's `adr-001-channel-adapter-as-aeos-kind.md`; the
AEOS-version bump promoting it into the core list is tracked there).

A `cmd` entry MAY declare `functions = [...]`, naming the ADR-164 node
functions that own the command (`acquire`, `transmit`, `control`, `ingest`,
`data_platform`, `front_end`, `assistant`). A node that declares its functions
shows the command only when one of these is among them, or when the operator
turns it on by name with `axi features enable`. A command with no `functions`
is owned by no function: it shows on a general install and is hidden on a
node with a role until turned on.

A node also declares its site's **agent policy** in the same node file,
`agents = "none" | "assist" | "local"`. A node with a role defaults to `none`;
a general install defaults to `local`, which is what every install already did.
Under `none` and `assist` no agent runs on the machine and nothing there calls a
language model: background registration is skipped, the autonomy switch cannot
override it, and the LLM gateway refuses before any request is sent
(`LLMRefusedByPolicy`). Turning features on never widens the policy, and
`features enable background-agents` is refused with the command that changes
it. The policy is changed only with `axi features agents <policy>`, a CLI-only
capability that is never offered to an MCP or agent client. `assist` differs
from `none` only off the machine: a maintenance agent elsewhere may propose
actions, which a person approves through the update approval channel.

The node file also declares how much **remote maintenance** the site accepts
(ADR-183), `maintenance = "off" | "requests" | "sessions"`, defaulting to `off`.
`requests` lets an operator send signed requests for allowlisted actions, which
the node fetches over its outbound connection and verifies itself; changes wait
for a person at the site. `sessions` adds support sessions that only a person at
the site can open and end. It is changed only with
`axi features maintenance <level>`, CLI-only like the agent policy, and `off`
stops it. `maintenance` and `support` are always visible on a node with a role.

A deployment definition carries the same declaration as `x-axiom-node` (including `maintenance`), with
`x-axiom-egress` (every outbound destination: host, port, purpose, data) and
per-service `x-axiom-listen`. `axiom.infra.disclosure` generates the "What runs
on this machine" page from it, and `disclosure.drift` fails when the definition
lists a listener or destination the published page does not.

Extension authors SHOULD prefer `[[extension.provides]]` form for any new declaration. The legacy `[[cli.commands]]` block has been removed entirely — manifests must declare CLI commands via `[[extension.provides]] kind = "cmd"`.

---

### 6.5 `[extension.availability]`: how a service stays up through a change

Required when an extension provides a `service`. ADR-182 states the rule: Axiom
does not go down from its own causes, meaning a deploy, an update, a schema
migration or a configuration change. This block is how each service says how
that holds for it, and `axi ext lint` refuses a service that says nothing.

| Key | Values | Meaning |
|---|---|---|
| `switch` | `blue-green`, `overlap`, `stateless-rolling` | `blue-green`: the new copy starts beside the old, passes readiness, traffic moves, the old drains. `overlap`: for a single-flight worker, the new copy starts on standby and takes the lease as the old one releases it, so work neither stops nor runs twice. `stateless-rolling`: copies are interchangeable and are replaced one at a time behind a balancer. `restart` is deliberately not a value: it is down for its duration. |
| `readiness` | `http:<path>`, `tcp`, `process` | How the switch knows the new copy can take traffic. An `http:<path>` readiness path of a served app is answered by the serving skeleton ahead of authz, unauthenticated, and returns only ready / not-ready (200 / 503) with no identity or data (ADR-182 D3); an app must not put its readiness behind its own authz. |
| `drain_s` | seconds, ≥ 0 | How long the old copy keeps serving in-flight work after traffic moves. |
| `migrations` | `expand-contract`, `none` | `expand-contract`: every schema change is split so the old and new versions can run against the same schema at once (add, backfill, switch, then remove in a later release). |
| `single_flight` | `true` / `false` | The service's work must never run twice (a scheduler, an orchestrator). Use `switch = "overlap"`. Exactly-once must hold per unit of work while two copies run together, enforced durably (for example the schedule fire log's unique claim), never by an in-process leader flag. A claim records the owning process (pid and start time), so a restarted copy recovers the work whose owner is dead on its own, with no signal, and leaves work whose owner is alive; the supervisor's SIGUSR1 (sent after the old copy exits) is the second path, for work with no recorded owner. |
| `verified_by` | `"pending"` or a path | The zero-gap upgrade test for this service, relative to the extension: 0 refused requests and 0 lost or duplicated work across a switch under load. |

A service that keeps a spool of pending items (a directory of work to finish
after a restart) must take a per-item claim with `axiom.infra.pending_claim`
before finishing any item in its resume step (ADR-182 D3a). Otherwise the new
copy of a switch finishes an item the old copy is still working on.

Lint codes: **AEOS100** a service with no block (error); **AEOS101** a malformed
block (error); **AEOS102** declared but `verified_by = "pending"` (warning, so
the fleet can declare honestly and prove one service at a time); **AEOS103**
`verified_by` names a file that does not exist (error, because it reads as
evidence).

A mounted route (a `MountSpec` on the composed HTTP app) runs inside the app's
process and switches when the app switches, so it declares the app's switch
and readiness and its own drain and migrations.

## 7. Python Packaging Integration

### 7.1 pyproject.toml alignment

The AEOS manifest coexists with `pyproject.toml`. Values in both files MUST agree where they overlap: name, version, description, license, repository, Python version constraints.

`pyproject.toml` declares the Python-specific build and runtime metadata; `axiom-extension.toml` declares the AEOS-specific capability metadata.

### 7.2 Entry points

Capability `entry` fields in the AEOS manifest map to Python entry points in `pyproject.toml`:

```toml
# pyproject.toml
[project.entry-points."axiom.agents"]
chalke = "classroom.agents.chalke:ChalkeAgent"

[project.entry-points."axiom.tools"]
syllabus_extraction = "classroom.tools.syllabus_extraction:SyllabusExtractor"

[project.entry-points."axiom.commands"]
enrollment = "classroom.commands.enrollment:cli"
```

At installation, Axiom loads entry points via the standard Python `importlib.metadata.entry_points()` mechanism. `axi ext lint` verifies that manifest `entry` values match registered entry points.

### 7.3 Public API

Every extension's `__init__.py` declares `__all__` enumerating the public symbols. Only these symbols may be imported by other extensions. All other symbols are private (Python convention: `_`-prefixed or in `_internal/` subpackage).

```python
# classroom/__init__.py
from classroom.agents.chalke import ChalkeAgent
from classroom.tools.syllabus_extraction import SyllabusExtractor

__all__ = ["ChalkeAgent", "SyllabusExtractor"]
```

### 7.4 Import-linter enforcement

At the monorepo root, `.importlinter` defines an `independence` contract over all extensions:

```ini
[importlinter]
root_packages = axiom, classroom, connect, memory, ...

[importlinter:contract:extension-independence]
name = "Extensions must not import each other's internals"
type = independence
modules = classroom, connect, memory, ...
```

Cross-extension imports are allowed only through declared public APIs. Violations block CI per ADR-031.

---

## 8. Testing Framework (axiom-tests)

### 8.1 Shared test plugin

`axiom-tests` is a dedicated PyPI package providing:
- Abstract base `TestCase` classes per capability kind
- Reusable pytest fixtures registered via `pytest11` entry point
- Property-based testing strategies via Hypothesis
- Mock services for integration tests (mock LLM, mock federation, mock IdP)

### 8.2 Standard test inheritance

Every extension has `tests/unit_tests/test_standard.py`:

```python
from axiom_tests.unit_tests import ExtensionStandardTests, ToolTests, AgentTests

class TestClassroomExtension(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self):
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"

class TestSyllabusExtractionTool(ToolTests):
    @pytest.fixture
    def tool_class(self):
        from classroom.tools.syllabus_extraction import SyllabusExtractor
        return SyllabusExtractor
    
    # Override capability properties that this tool supports
    @property
    def supports_streaming(self) -> bool:
        return False
```

### 8.3 Opt-in capability properties

Standard test base classes expose capability properties defaulting to `False`. Extensions override to declare support, which activates the relevant tests.

### 8.4 Mock services

`axiom-tests` provides fixtures for integration tests without requiring live services:
- `mock_llm` — canned LLM responses
- `mock_federation` — fake federation peer
- `mock_oidc` — fake OIDC IdP
- `mock_registry` — fake Vyzier registry
- `tmp_axiom_home` — isolated `~/.axiom/` directory per test

### 8.5 Integration tests

`tests/integration_tests/` exercises the extension against real services. CI runs these behind a flag. Extensions declare required external services in their manifest:

```toml
[extension.testing.integration_tests]
requires_services = ["postgresql", "canvas_lms_sandbox"]
```

---

## 9. Signing and Attestation

### 9.1 Mandatory signing

Every published AEOS extension MUST be signed via Sigstore's keyless OIDC flow. The publisher authenticates via their GitHub/Google/institutional OIDC; Sigstore issues a short-lived certificate; the artifact and signature are published together.

### 9.2 Verification at install

`axi ext install <name>` verifies the Sigstore signature against the declared publisher identity before installing. Mismatches abort the install with a clear error.

**Implementation status (ADR-109 second half, closed 2026-09-19).** The remote install source (`axi ext install <name> --from-pypi`) verifies the distribution's PEP 740 provenance attestation — Sigstore keyless: Fulcio certificate chain, Rekor inclusion, artifact digest — against an expected publisher identity *before* `pip install` runs. The expected identity resolves from, in order: an explicit `--publisher owner/repo[@workflow]` override, an operator pin in `$AXIOM_HOME/trusted-publishers.json`, or the harness's built-in pins for its own distributions; an undeclared identity refuses rather than trusting first use. Refusals are categorized and named in the error: identity mismatch, missing attestation, network failure, invalid attestation, or missing verifier dependency — a harness without the verifier installed (`pypi-attestations`, the `verify` extra) refuses remote installs and names the dependency; it never skips verification silently. The `--no-verify` escape hatch warns loudly and appends a receipt to `$AXIOM_HOME/install-verify-bypass.log`. Local `file://` sources (the v0.1 registry and `--from-url`) are exempt — no PyPI provenance exists for a local tarball — and the install output states the exemption; those paths keep the ed25519 detached-signature verification of §9.1's local backend. Implementation: `axiom/cli/ext/provenance.py`.

### 9.3 Publisher identity

The AEOS manifest's `signing.publisher_identity` field declares the expected signer. Verification compares the Sigstore certificate's subject to this field.

### 9.4 Behavioral attestation (AEOS leap-ahead)

Beyond signing, AEOS supports behavioral attestation. A running AEOS runtime observes an extension's actual behavior over time and issues an attestation: "at time T, extension X's observed behavior matched its declared capabilities with confidence C." Attestations are signed by the observing runtime and are consumable by other AEOS runtimes for trust decisions.

Behavioral attestation is optional for extensions but required for installation in high-security deployments (classification ceiling > "restricted").

### 9.5 Quarantine and recovery

If an extension's behavior diverges from its declared capabilities (via behavioral classification of drift), an AEOS runtime MAY quarantine the extension. Quarantined extensions remain installed but execute only in a restricted mode pending recovery ceremony: the publisher re-signs an updated release, the updated attestation is verified against observed behavior, and the runtime lifts quarantine.

This is a direct response to the ClawHavoc incident: an extension that was legitimate at install can drift into malicious behavior; AEOS detects and contains rather than requiring scorched-earth deletion.

---

## 10. CLI Surface

AEOS specifies an `axi ext` command group with three tiers of functionality. A conformant harness implements Tier 1 and Tier 2 at minimum. Tier 3 is strongly recommended. Tier 4 is Axiom-specific and optional for non-Axiom implementations.

### 10.1 Tier 1 — Lifecycle (required)

| Command | Purpose |
|---|---|
| `axi ext init <name>` | Scaffold a new compound extension with canonical layout |
| `axi ext templates` | List available extension templates |
| `axi ext install <name>` | Install from Vyzier (planned / not yet built) or PyPI fallback |
| `axi ext uninstall <name>` | Remove an installed extension, preserving audit trail |
| `axi ext update [<name>]` | Update one or all installed extensions |
| `axi ext list` | Show installed extensions, versions, status |
| `axi ext search <query>` | Discover extensions in registry |
| `axi ext show <name>` | Detailed view of one extension |

### 10.2 Tier 2 — Quality (required)

| Command | Purpose |
|---|---|
| `axi ext lint` | Conformance check (layout, manifest, imports, entry points) |
| `axi ext validate` | Deeper check: load extension, run standard tests, verify declarations |
| `axi ext test` | Run extension's own test suite via axiom-tests |
| `axi ext doctor [<name>]` | Diagnose issues (missing files, misconfiguration, outdated deps) |
| `axi ext docs` | Generate documentation from schemas, manifest, SKILL.md files |
| `axi ext config <name> [get\|set]` | Manage extension-specific settings |
| `axi ext publish` | Sign with Sigstore, publish to Vyzier (planned / not yet built) via Trusted Publisher |
| `axi ext sign` | Sign a local artifact |
| `axi ext verify <artifact>` | Verify a signed artifact |
| `axi ext scan` | Static security analysis (behavioral classification) |
| `axi ext graph` | Dependency graph of installed and declared extensions |
| `axi ext run <name> [args]` | Execute extension directly (for testing) |
| `axi ext eval` | Run evaluation suite (Langfuse-scored when configured) |
| `axi ext migrate` | Upgrade extensions to current AEOS spec version |

### 10.3 Tier 3 — Development experience (strongly recommended)

| Command | Purpose |
|---|---|
| `axi ext dev [<name>]` | Watch mode: hot-reload on change, opens playground if configured |
| `axi ext studio [<name>]` | Visual playground / debugger (analogous to Mastra Studio, LangGraph Studio, Microsoft DevUI) |
| `axi ext replay <execution-id>` | Re-run a past execution from trace |
| `axi ext trace <execution-id>` | Observability deep-dive for an execution |
| `axi ext bench` | Performance benchmark against baseline |
| `axi ext train` | Training loop (for extensions with learned behavior, e.g., behavioral-classifier-backed classifiers) |
| `axi ext deploy <target>` | Deploy extension to a target (Axiom Cloud, Kubernetes, etc.) |

### 10.4 Tier 4 — Leap-ahead (Axiom-specific, AEOS-exclusive)

These commands depend on Axiom's federation, behavioral classification, and Vega features. Non-Axiom AEOS implementations may provide equivalents or omit.

| Command | Purpose |
|---|---|
| `axi ext federate <name>` | Propagate extension across federation with trust inheritance |
| `axi ext attest <name>` | Generate behavioral attestation |
| `axi ext quarantine <name>` | Quarantine per ADR-025 threat model |
| `axi ext recover <name>` | Initiate recovery ceremony (Vega-native) |
| `axi ext evolve [<name>]` | Self-improvement loop (governed through RACI) |
| `axi ext govern <name>` | Apply a trust profile to an installed extension |

---

## 11. Factory / Provider Pattern for CLI Commands

Per Axiom's "everything is an extension" convention, every `axi ext` command is itself an extension implementing a Provider interface.

### 11.1 Provider interface

```python
# axiom.ext_cli.provider
from typing import Protocol

class ExtCliProvider(Protocol):
    """Provider interface for an `axi ext <verb>` command."""
    
    verb: str                   # e.g., "install", "lint", "publish"
    description: str
    
    def add_arguments(self, parser: argparse.ArgumentParser) -> None: ...
    def run(self, args: argparse.Namespace, context: CliContext) -> int: ...
```

### 11.2 Default built-in providers

B-Tree Labs ships default implementations for Tier 1, Tier 2, and Tier 4 verbs. Tier 3 verbs (studio, dev, replay, etc.) ship as separate extensions that users opt into.

### 11.3 Override and extend

Third parties can register alternative providers for any verb. Example: a security vendor could provide a custom `axi ext scan` implementation that runs their scanner instead of Axiom's default behavioral-classification one. The `pyproject.toml` entry point registration determines precedence:

```toml
[project.entry-points."axiom.ext.cli.providers"]
scan = "my_security_vendor.ext_scan:MySecurityScanProvider"
```

### 11.4 Capability-aware defaults

Providers discover what's available at runtime. For example, `axi ext federate` requires vega-trust to be installed; if not, the command emits a clear "vega-trust required for federation; install with `axi ext install vega-trust`" rather than failing opaquely.

---

## 12. Conformance Levels

AEOS defines three conformance levels. Extensions and harnesses declare their level.

### 12.1 Bronze — Compatibility

- Manifest validates against AEOS schema
- Layout conforms (compound directory structure)
- All required files present
- Tier 1 + Tier 2 CLI commands operate correctly

### 12.2 Silver — Signed and Tested

- All Bronze requirements
- Extension is Sigstore-signed
- Extension has standard-test coverage ≥ 80%
- Public API declared and enforced via import-linter

### 12.3 Gold — Federation-Ready

- All Silver requirements
- Extension supports behavioral attestation
- Extension supports quarantine/recovery ceremony
- Extension declares trust-profile requirements and classification ceiling
- Tier 4 CLI commands operate correctly for this extension

### 12.4 Chain readiness (separate axis, 0.2 draft)

Bronze, Silver and Gold grade packaging, signing and federation. Chain readiness
(Level A, B or C, §4.11.2) grades how an extension's skills describe their relation
to one another, and is claimed and verified independently. Neither implies the other.

Axiom's `axi ext lint` reports the conformance level achieved. Production Axiom deployments may require Gold conformance for any extension handling classification > "public."

---

## 13. Registry and Distribution

### 13.1 Vyzier as the canonical AEOS registry (planned / not yet built)

Vyzier (planned / not yet built) is B-Tree Labs' polyglot extension marketplace and the canonical registry for AEOS extensions. Extensions published to Vyzier:
- Are verified for AEOS conformance at least at Bronze level
- Are served with Sigstore signatures
- Are searchable via `axi ext search`
- Carry metadata about publisher identity, conformance level, federation shareability

### 13.2 Alternative registries

AEOS does not mandate Vyzier. Other AEOS-conformant registries may exist. `axi ext` commands accept `--registry <url>` to target alternatives. Conformance checks are identical regardless of registry.

### 13.3 PyPI as fallback

For extensions that do not use Vyzier (e.g., private extensions, research artifacts), `axi ext install <name>` falls back to PyPI. The AEOS manifest is discovered in the installed package rather than via registry metadata.

### 13.4 Compatibility with MCPB

AEOS extensions whose only exposed capability is one or more MCP tools are packaged as MCPB-compatible archives. An MCPB client can install the extension using MCPB tooling; an AEOS client uses AEOS tooling. The two are compatible at the archive level.

### 13.5 Registry entry descriptor (server.json profile)

A registry entry — whether a connector, an extension, a pack, or an inference resource — is described by a **profile of MCP's `server.json`** rather than a bespoke schema, so an AEOS registry is a conformant **sub-registry** of the MCP Registry rather than a fork (see ADR-074):

- **Identity** is reverse-DNS (`ai.axiom.connector.slack`) + semver `version`, per `server.json`.
- **Inputs** use `server.json`'s `environmentVariables[]` with `{name, description, isRequired, isSecret}`. A secret-declared variable **never carries a value** — it names a binding resolved from the keystore (KEEP / `secret_ref`), never inline.
- **Axiom-specific fields ride in the reverse-DNS `_meta` namespace** `ai.axiom.registry/*` — never as forked top-level keys. The Axiom profile defines:
  - `ai.axiom.registry/artifact_class` ∈ {`connector`, `extension`, `pack`, `inference_resource`} — the top-level class; each class carries its own installer and risk tier.
  - `ai.axiom.registry/kind` — the sub-type within a class (e.g. `channel_adapter`, `source_kind`, `secret_backend`).
  - `ai.axiom.registry/trust_tier` ∈ {`first_party`, `verified`, `certified`, `community`} — a graduated trust ladder (cf. Power Platform), not binary listed/denied.
  - `ai.axiom.registry/connection_ref` — the **connection** (credentials + health) the entry binds to. AEOS keeps the **connection (auth instance) separate from the connector (definition)**, mirroring the universal connection-vs-connector split.
  - `ai.axiom.registry/classification` and `…/egress` — the policy/EC inputs; an external-egress entry is auto-flagged for stricter consent (the AEOS leap-ahead over §2's standards, none of which carry EC classification).

### 13.6 Federated sub-registry topology

AEOS adopts the MCP Registry's **central-source + federated-sub-registry** model, not a global DHT. An AEOS registry (Vyzier, or a private enterprise instance) ingests upstream catalogs (the MCP Registry, an OASF Agent Directory) as feeds and overlays Axiom-native entries plus local policy/curation. Entries are content-addressed and Sigstore-signed (§9, §14); cohort scoping, trust-graph gating, and classification ceilings (§14) decide which entries an agent may discover and pull. The dependency direction is consume-only: a private registry never has to publish upstream.

---

## 14. Federation-Native Features (AEOS Leap-Ahead)

These features differentiate AEOS from competing standards. They require an AEOS-conformant runtime (currently Axiom with Vega).

### 14.1 Federation distribution

AEOS extensions declare `federation.shareable = true` in their manifest to opt into federation distribution. Shared extensions travel through the Vega manifest channel with signed attestations. Receiving nodes verify signatures and trust-profile compatibility before activation.

### 14.2 Trust inheritance

An extension's trust status can be inherited from:
- Publisher identity (institutional signer)
- Parent extension (for forks)
- Federation membership (trusted peer's attestation)

Trust inheritance is per-relationship: wrapper extensions inherit full trust; forks inherit partial; compositions inherit from all parents.

### 14.3 Behavioral attestation

Per Section 9.4. AEOS runtimes observe extension behavior, compare to declared capabilities, and issue attestations. Attestations propagate through federation, enabling cross-node trust decisions based on actual observed behavior.

### 14.4 Validated classification

Extensions declare a classification ceiling. AEOS runtimes validate that actual data flow through the extension respects the ceiling. Violations trigger quarantine.

### 14.5 Quarantine and recovery

Per Section 9.5. Quarantined extensions remain installed but run in restricted mode. Recovery requires publisher re-signing with updated attestation.

---

## 15. Implementation Roadmap

### Phase 1 — Internal reference implementation (Axiom, next 6 weeks)

- Axiom implements AEOS 0.1 fully for Tier 1, Tier 2, Tier 4 commands
- `axi ext init`, `lint`, `validate`, `publish`, `install`, etc. operational
- All Axiom, Keplo, Vyzier, and consumer extensions migrate to AEOS-conformant layout (per ADR-031)
- `axiom-tests` package shipped
- AEOS JSON Schema published in Axiom repo

### Phase 2 — Tier 3 commands (post-Prague, Q4 2026)

- `axi ext studio`, `dev`, `replay`, `trace`, `bench`, `train`, `deploy`
- Visual playground, hot-reload dev server

### Phase 3 — Conformance testing & certification (Q1 2027)

- Published AEOS conformance test suite
- Self-certification tooling for extension publishers
- Conformance badges for Bronze/Silver/Gold levels

### Phase 4 — Public-track contributions (rolling)

- Identify OASF contribution opportunities for capability taxonomy
- Identify MCPB contribution opportunities for MCP-server-subset AEOS extensions
- Submit contributions to AAIF where strategic per ADR-032

### Phase 5 — Strategic review (quarterly)

- Evaluate donation/publication triggers per ADR-032
- Release AEOS chunks to public standards bodies when triggers met
- Continue morphing AEOS privately while other pieces go public

---

## 16. Open Questions

- Should AEOS 0.2 include a WASM capability kind (stateless, sandboxed)?
- How does AEOS interact with TypeScript/Node extensions (Mastra-style)? Current spec is Python-centric; v0.2 should define a cross-language layer.
- What's the right versioning cadence for AEOS itself? Major versions likely break extensions; we should avoid them for 12-18 months.
- When 0.2 is ratified, should chain level B become a condition of Silver for extensions that expose destructive skills over MCP?
- Should Level C also require a measured benefit (fewer refused calls) before it is claimed, or stay a structural claim?
- Should attestations be transferable across federations (i.e., UT's attestation counts at INL)? Trust-graph topology affects this.

---

## 17. Revision History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1.0 | 2026-04-21 | Benjamin Booth | Initial draft |
| 0.2.0-draft | 2026-10-08 | Benjamin Booth | Additive: §4.11 chain readiness (levels A, B, C; declarations in the skill table; MCP mapping; the AEOS delta contracts; rule catalogue AEOS100 to AEOS150), §4.12 recipe (runtime-consumed kind), `chain_level` in `[extension]`, §12.4. No 0.1 manifest changes meaning. |
