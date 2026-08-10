# Agent Extension Open Standard (AEOS) — Version 0.1

**Status:** Draft — Internal (per ADR-032 dual-track strategy; intended for eventual public release pending strategic decisions)
**Version:** 0.1.0
**Editor:** Benjamin Booth
**Date:** 2026-04-21
**Reference implementation:** Axiom platform (b-tree-labs/axiom-os)

---

## Abstract

The Agent Extension Open Standard (AEOS) defines a portable, signed, cross-harness package format and runtime contract for agentic AI extensions. An AEOS-conformant extension bundles one or more capability kinds — agents, tools, CLI commands, services, adapters, skills, and hooks — under a single manifest with declared dependencies, standardized test bases, and cryptographic provenance. AEOS wraps and extends existing industry standards (MCP, A2A, SKILL.md, AGENTS.md, OpenAPI, Sigstore) rather than replacing them. Extensions using only AEOS's public-standard subset are interoperable with any harness implementing those standards; extensions using AEOS's federation, attestation, and governance features require an AEOS-conformant runtime.

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
| **AGENTS.md** | AEOS extensions at the repository level provide AGENTS.md guidance for coding agents operating on the source. |
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

Every extension is scaffolded with the canonical compound layout (typed subdirectories for each capability kind). Extensions that ultimately provide only one capability kind are compound extensions with only one populated subdirectory. This uniformity removes the decision burden at authoring time.

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

AEOS defines exactly seven capability kinds. This list is extensible by future AEOS versions but closed within a given version.

### 4.1 agent

An LLM-backed autonomous component with persistent identity and state across sessions. Agents have names in the Axiom AXI convention (ALL-CAPS-HYPHEN: AXI, SCAN, TIDY, PRESS, TRIAGE, CURIO, CHALKE, WARDEN).

- Provides: `classify`, `plan`, `execute`, `learn` (depending on agent role)
- Declares: `name`, `description`, `allowed_tools`, `uses_skills`, `requires_signals`, `state_model`

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

`axi ext lint --strict` makes these errors.

---

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

- `AGENTS.md` — strongly recommended for any extension with non-trivial source
- `docs/` — strongly recommended; required for any extension with ≥2 capability kinds
- Capability-kind subdirectories populated per what the extension provides
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

Extension authors SHOULD prefer `[[extension.provides]]` form for any new declaration. The legacy `[[cli.commands]]` block has been removed entirely — manifests must declare CLI commands via `[[extension.provides]] kind = "cmd"`.

---

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
| `axi ext install <name>` | Install from Vyzier (or PyPI fallback) |
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
| `axi ext publish` | Sign with Sigstore, publish to Vyzier via Trusted Publisher |
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

Axiom's `axi ext lint` reports the conformance level achieved. Production Axiom deployments may require Gold conformance for any extension handling classification > "public."

---

## 13. Registry and Distribution

### 13.1 Vyzier as the canonical AEOS registry

Vyzier is B-Tree Labs' polyglot extension marketplace and the canonical registry for AEOS extensions. Extensions published to Vyzier:
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
- Should attestations be transferable across federations (i.e., UT's attestation counts at INL)? Trust-graph topology affects this.

---

## 17. Revision History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1.0 | 2026-04-21 | Benjamin Booth | Initial draft |
