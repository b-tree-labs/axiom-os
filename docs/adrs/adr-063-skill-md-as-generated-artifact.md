# ADR-063 — SKILL.md as generated artifact from `SkillRegistry`

**Status:** Accepted — 2026-06-01
**Owner:** @ben
**Related:** ADR-056 (skill-as-function), ADR-058 (Agent Standards Registry), spec-aeos-0.1 (`kind = "skill"` block requires `entry` + `path`), competitive-parity-gaps 2026-06-01

## Context

ADR-056 made every CLI verb a thin wrapper over a function registered through `axiom.infra.skills.SkillRegistry`. v0.30 fully landed this for PRESS (7 skills) and prior cuts for data_platform, hygiene, RIVET, etc.

But two surfaces need each skill described:

| Surface | Where it lives today | Fields required |
|---|---|---|
| **`SkillRegistry`** (Python) | Per-skill `register_skill(...)` call in each ext's `skills/__init__.py` | name, description, params shape, function ref |
| **`axiom-extension.toml`** AEOS manifest | Per-ext `[[extension.provides]]` block with `kind = "skill"` | name, `entry`, `path` (per spec-aeos-0.1 §5) |
| **`SKILL.md`** (Anthropic Skills / OASF format) | Not yet authored for any Axiom ext | YAML frontmatter (name, description, allowed-tools) + body |

Today only the first exists. The v0.30 PRESS manifest declared 7 skill `provides` blocks then removed them because the AEOS schema rejected blocks missing `entry` + `path`. The skills are SkillRegistry-callable but invisible to MCP discovery, OASF catalogs, and the AEOS `provides` enumeration.

Hand-authoring SKILL.md per skill duplicates name + description + params shape three ways. Drift is guaranteed within a quarter. We've seen this before with persona docs vs `axi roles list` — they drifted within weeks.

## Decision

**`SkillRegistry` registration is the single source of truth.** SKILL.md files are generated from it and committed to the repo; a CI lint asserts checked-in SKILL.md ≡ generated SKILL.md.

### The generator

`axi skills emit-md [--ext <name>] [--check]` walks every ext's `skills/` package, imports each registered `SkillSpec`, and emits one `SKILL.md` per skill at the AEOS-prescribed path:

```
src/axiom/extensions/builtins/<ext>/skills/<skill_name>/SKILL.md
```

Frontmatter format (compatible with both Anthropic Skills and OASF intent):

```markdown
---
name: <skill_name>
description: <from SkillSpec.description>
version: <from ext manifest>
inputs:
  - <param shape rendered from SkillSpec.params_schema>
outputs:
  - kind: SkillResult
allowed-tools: <from SkillSpec.allowed_tools, default [] >
---

<body sourced from SkillSpec.long_description if present, else
a stub pointing to the Python function via `entry`>
```

The matching `axiom-extension.toml` block is *also* emitted by the generator into a dedicated `[[extension.provides]]` section per skill, satisfying the AEOS `entry` + `path` requirement without hand-edits.

### The lint

`axi ext lint --skills` (and `--check` mode of `emit-md`) compares the on-disk SKILL.md + manifest blocks against the generated output. Diff means PR fails. Same pattern as `ruff --check` vs `ruff --fix`.

### The Python side

A small `@skill_meta` extension to the existing skill registration carries the previously-undeclared fields:

```python
register_skill(
    name="press.draft",
    fn=draft,
    description="Render markdown to docx in the source's scope.",
    long_description="Full operator-facing prose...",   # NEW
    inputs={"source": "Path", "format": "str = 'docx'"},  # NEW (shape-only)
    allowed_tools=(),                                     # NEW
)
```

Existing call sites stay valid (new fields default empty); the generator emits a minimal SKILL.md when prose isn't present.

## Consequences

**Wins**
- One source of truth eliminates the three-way drift problem before it starts.
- AEOS `provides` enumeration becomes complete (the v0.30 PRESS regression closes).
- MCP discovery + OASF catalogs both find Axiom skills the moment we choose to publish (gated by consumer production validation per the parity-gaps doc — generator runs locally even before public publishing).
- Pattern extends uniformly: consumer extensions (and Keplo, Vyzier when they exist) get the same `axi skills emit-md --check` lint by composition. Nothing per-consumer to invent.

**Costs**
- Every ext touches its `register_skill` calls to add `long_description` + `inputs` + `allowed_tools` once. PRESS has 7; data_platform, hygiene, RIVET each have 3-8. Estimated ~50 skill specs across Axiom; ~10-15 across current consumer extensions. Mechanical edit.
- Adds a generator + lint step to CI. Minor; mirrors `ruff` + `mypy` shape.

**Non-goals**
- Publishing to a public MCP catalog (parked per parity-gaps; awaits consumer production validation).
- Authoring rich prose for every skill body. Generator emits minimal stub if `long_description` absent; authors fill in over time. Lint doesn't require prose, only that frontmatter matches Python.

## Rollout

| PR | Scope |
|---|---|
| PR-1 | Generator + lint + ADR-056 `SkillSpec` field additions + first 3 PRESS SKILL.md files as exemplars |
| PR-2 | Backfill remaining 4 PRESS skills + data_platform skills + hygiene skills |
| PR-3 | Backfill RIVET + remaining Axiom extensions; flip CI lint to required |
| PR-4 (consumer repo) | Run generator across the consumer's extensions; commit; flip the consumer's CI lint |

Per PR-2/3 batching, completing the Axiom side is ~1 day of focused work; the consumer PR-4 is another half-day.
