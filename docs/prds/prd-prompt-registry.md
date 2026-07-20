# Prompt Template Registry — PRD

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-20
**Layer:** Axiom core
**Tech Spec:** [spec-prompt-registry.md](../tech-specs/spec-prompt-registry.md)

---

## The Problem

Every prompt sent to an LLM in this system is assembled ad-hoc in code. There is
no version history, no audit trail, no way to A/B test wording changes, and no
mechanism to swap a few-shot example block without cutting a code release.

### Specific failure modes today

**Hardcoded security preamble.** `gateway.py` contains `_EC_HARDENED_PREAMBLE`, a
string constant defining the non-negotiable security policy injected into every
export-controlled completion. It has no version number. When it changes, git blame
is the only audit trail. There is no test to catch regressions introduced by
rewording it.

**Ad-hoc agent system prompts.** Each agent assembles its system prompt inside its
`run()` method — string concatenation, f-strings, conditionals. Neut, SCAN, TIDY,
PRESS, and TRIAGE each do this independently. Changes to one agent's persona are
invisible to operators reviewing agent behavior in production logs.

**No prompt caching.** The Anthropic API supports `cache_control: {type: "ephemeral"}`
on message blocks, enabling the API to cache static content (preambles, persona
definitions, tool descriptions) across requests. None of this is exploited today.
At agentic loop scale — dozens of tool calls per session, hundreds of sessions per
day — uncached static content is a measurable cost and latency overhead.

**No regression testing for prompts.** When a prompt changes, there is no mechanism
to replay past sessions against the new version to detect behavioral regressions.
The system has no record of which prompt text produced which completion.

**No operator visibility.** An operator reviewing a session log cannot determine
what system prompt the agent was operating under at the time of a given completion.
This is a compliance gap in any deployment requiring audit capability.

---

## What the Prompt Registry Does

The Prompt Template Registry replaces hardcoded and ad-hoc prompt construction
with a managed artifact system. Prompt templates are named, versioned TOML
documents that compose at runtime — not at deploy time.

### Core capabilities

| Capability | Description |
|---|---|
| **Named templates** | Every prompt has a stable `id` (e.g., `ec_hardened_preamble`, `axi_agent_persona`). References in code are IDs, not strings. |
| **Versioned artifacts** | Every change to a template increments its version. Old versions remain accessible. |
| **Layered composition** | Templates compose by layer: Axiom base → domain → extension → per-request override. Same model as the glossary system. |
| **Variable interpolation** | Templates use `{{variable_name}}` slots for dynamic content. Static structure is separate from dynamic injection points. |
| **Cache hints** | Each template declares `cache_hint = true/false`. The gateway uses this to apply `cache_control: {type: "ephemeral"}` on static blocks. |
| **Audit integration** | Every completion log record includes the template ID, version, and content hash used. |

---

## Template Anatomy

A prompt template is a TOML record with the following fields:

| Field | Type | Description |
|---|---|---|
| `id` | string | Stable identifier. snake_case. Referenced throughout the codebase. |
| `layer` | string | `"axiom"` \| `"domain"` \| `"extension"` |
| `role` | string | `"system"` \| `"user"` \| `"assistant"` |
| `content` | string | Template body. Use `{{variable_name}}` for interpolation slots. |
| `version` | string | Semver string (e.g., `"1.0.0"`). |
| `cache_hint` | bool | If `true`, static content eligible for Anthropic prompt caching. |
| `tags` | list[string] | Free-form labels for filtering and discovery. |
| `see_also` | list[string] | Related template IDs. |
| `extends` | bool | If `true`, content appends to a same-id template at a lower layer. If `false` (default), this template replaces the lower-layer version. |

Templates that contain only static text (no `{{...}}` slots) and `cache_hint = true`
are the primary targets for Anthropic prompt caching. Variable slots imply dynamic
content and should not be cached.

---

## Composition Model

Templates compose in the same layer order as the glossary system:

```
Axiom base  →  domain layer  →  extension layer  →  per-request override
(lowest)                                              (highest)
```

When two templates share the same `id` at different layers:

- If the higher-layer template has `extends = true`, its content is **appended**
  to the lower-layer content. This is the correct pattern for domain-specific
  addenda to a base preamble.
- If the higher-layer template has `extends = false` (the default), it **replaces**
  the lower-layer template entirely.

Per-request overrides are passed as raw strings to `complete_with_tools()` and
are not stored in the registry. They are always injected after composed template
content and are never cached.

The composed result — the final string after all layers are merged and variables
are interpolated — is what gets sent to the LLM. The `ComposedPrompt` object
carries the content hash of this final string, enabling bit-exact audit comparison.

---

## Use Cases

### EC hardened preamble
The `_EC_HARDENED_PREAMBLE` constant in `gateway.py` migrates to a template with
`id = "ec_hardened_preamble"`, `layer = "axiom"`, `cache_hint = true`. It is fully
static (no variables). Every EC-gated completion will cache this block on the first
call of a session, with subsequent calls in that session hitting cache.

### Agent persona system prompts
Each agent declares a `system_prompt_template` in its `axiom-extension.toml`
manifest. The gateway resolves this template at call time. Neut, SCAN, TIDY, PRESS,
and TRIAGE each have their own persona template, independently versioned. Domain
extensions can overlay or extend any of these without modifying builtin code.

### Few-shot example sets
A set of few-shot examples (e.g., signal extraction examples for SCAN) is a template
with `cache_hint = true`. It is static content that changes infrequently. Caching
it across sessions in an agentic loop avoids re-sending hundreds of tokens on every
tool call.

### Tool description blocks
When a long list of tool descriptions is injected into a system prompt, that list
is static for the duration of a session. Representing it as a cacheable template
block reduces per-call overhead in tool-heavy agents.

### Per-request context injection slots
Dynamic content (retrieved RAG chunks, session state summaries, user-provided
context) is injected via `{{variable_name}}` slots in templates. These slots are
always non-cached. Separating dynamic slots from static structure maximizes cache
hit rate.

---

## Prompt Caching

The Anthropic API supports up to 4 cache breakpoints per request. Content at each
breakpoint is cached for 5 minutes (ephemeral). Cached content does not count
toward input token billing on subsequent calls.

The registry enables systematic use of this capability:

1. Templates with `cache_hint = true` are identified at load time.
2. The gateway orders message blocks so that cached (static) blocks appear before
   dynamic blocks. This is required by the Anthropic cache semantics: content
   before a cache breakpoint must be identical across requests for the cache to hit.
3. At operator-session scale (dozens of tool calls per session), caching the
   security preamble, agent persona, and tool list on the first call of a session
   means subsequent calls in that session pay only for the dynamic tokens.

This is not a micro-optimization. For agentic loops — where the same system prompt
accompanies 20–50 tool calls in a single session — caching static blocks is a
significant cost and latency reduction. The registry makes this structural rather
than requiring manual per-call bookkeeping.

---

## Versioning and Audit

Every template change produces a new version. The prior version remains accessible
in the registry (old template files are not deleted; they are tracked in version
control).

Every completion log record includes:
- `prompt_template_id` — the resolved template ID
- `prompt_template_version` — the version string at call time
- `prompt_content_hash` — SHA-256 of the composed, interpolated content

This enables two audit capabilities:

**Change detection.** If behavior changes after a template update, the log diff
between old and new `prompt_template_version` and `prompt_content_hash` pinpoints
exactly what changed and when.

**Regression testing.** Past sessions can be replayed against a new template
version (holding all other inputs constant) to detect behavioral regressions before
promoting the new version to production. This is the foundation of a prompt
regression test suite.

---

## CLI

The `axiom prompt` noun provides operator access to the registry. It is implemented
as a utility extension at `src/axiom/extensions/builtins/prompt_registry/`.

| Command | Description |
|---|---|
| `axiom prompt list` | List all templates. Accepts `--layer` and `--tags` filters. |
| `axiom prompt show <id>` | Print the current version of a template, including all metadata. |
| `axiom prompt show <id> --version <v>` | Print a specific historical version. |
| `axiom prompt diff <id> <v1> <v2>` | Unified diff between two versions of a template. |
| `axiom prompt validate` | Check all templates for schema conformance, broken `see_also` references, and undefined `{{variable}}` slots with no default. |

These commands are read-only. Template creation and modification is done via
direct TOML file editing and version control — not through a write API. This
ensures templates are code-reviewed artifacts, not ad-hoc database entries.

---

## Relationship to Agents

Each agent extension declares its system prompt template in `axiom-extension.toml`:

```toml
[agent]
system_prompt_template = "axi_agent_persona"   # resolved from registry at call time
```

The gateway resolves this ID at call time. If the template does not exist, the
call fails with a clear error — not a silent fallback to an empty prompt. This
makes misconfiguration visible immediately.

Domain extensions that install custom agents follow the same pattern. They ship
their own `prompt-templates.toml` alongside their extension manifest. Discovery
and composition follow the standard layer order.

---

## Implementation Phases

### Phase 1 — Foundation (target: current sprint)

- TOML registry schema finalized.
- `TemplateRegistry` loader implemented in `src/axiom/infra/prompt_registry.py`.
- `gateway.py` updated to accept `system_template_id` parameter.
- `_EC_HARDENED_PREAMBLE` constant migrated to `docs/prompt-templates-axiom.toml`
  as template `ec_hardened_preamble`.
- Existing agent `run()` methods refactored to declare template IDs in their
  manifests rather than assembling prompts inline.
- Backward compatibility maintained: if `system_template_id` is `None`, raw
  `system` string is used as before. No breaking change to existing callers.

### Phase 2 — Prompt caching (follow-on)

- Cache hint resolution implemented in gateway.
- Static template blocks (preamble, persona, tool descriptions) wired to
  Anthropic `cache_control: {type: "ephemeral"}`.
- Cache hit rate tracked in completion logs.

### Phase 3 — Versioning and regression (follow-on)

- Template version history accessible via `axiom prompt show --version`.
- `axiom prompt diff` command implemented.
- Session replay harness for prompt regression testing.
- Prompt template ID, version, and content hash written to every completion log
  record.

---

## What This Does Not Cover

- **Dynamic prompt generation** (LLM-writes-prompt-for-LLM) is out of scope.
  This registry manages human-authored templates, not machine-generated ones.
- **Template storage backends** other than the filesystem. Templates are TOML
  files tracked in version control. There is no database backend, no API server,
  no remote registry.
- **A/B testing infrastructure.** The registry enables the data collection
  (which template version produced which completion) that makes A/B analysis
  possible, but the analysis tooling is a separate concern.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
