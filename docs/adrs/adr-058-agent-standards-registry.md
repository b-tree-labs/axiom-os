# ADR-058: Agent Standards Registry

**Status:** Accepted
**Date:** 2026-06-01
**Deciders:** Benjamin Booth
**Related:** ADR-055 (Governance Fabric), ADR-056 (Skill as Function), ADR-057 (Connector Primitive), ADR-059 (Connector-First Vendor Unification), ADR-060 (Cross-Agent Event Routing), `docs/working/axiom-v0.30-unified-fabric-plan.md`

---

## Context

Operators express intent at a high altitude: *"Do this the standard way."* Today the answer lives in implicit code conventions, prose docs, and tribal knowledge — there is no addressable, queryable, invocable target for "the standard way to do X."

Concrete situations driving this:

- Generating a PRD docx involves: source-scope detection (where the output should land), non-clobbering naming, mirror directory structure, footer-metadata stamping, mermaid pre-rendering, link rewriting, optional version-suffix from metadata. These conventions live in code in `PublisherEngine.generate` and adjacent helpers. An operator (or agent) cannot ask "what is PRESS's standard for generating a PRD?" and get a structured answer.
- HERALD has channel adapters, recipient preferences, classification routing, and dedup. The combination "send an urgent compliance alert" has a standard shape — but it is not declared.
- RIVET has 8 skills (artifact_quota, test_pollution, trunk_health, etc.). Each fires under its own cadence and severity. There is no named bundle "the standard CI sweep" — operators reason about it via memory.

Without a registry:
- Peer agents over A2A cannot consult another agent's standards.
- External harnesses (Claude Code, Cursor, ChatGPT Desktop) over MCP cannot say "ask Axiom to publish this the standard way."
- New maintainers cannot inspect what conventions PRESS enforces without reading code.

## Decision

Each agent persona publishes a **Standards Registry**: a mapping from a stable, operator-readable name to an ordered sequence of skill invocations.

```python
# publishing/standards.py
STANDARDS: dict[str, AgentStandard] = {
    "publish_prd": AgentStandard(
        description="Generate a PRD draft to the source's filesystem scope,"
                    " non-clobbering, mirror structure, footer-stamped.",
        skills=[
            ("press.detect_version", {}),
            ("press.generate",       {"footer_from_metadata": True}),
        ],
    ),
    "publish_for_review": AgentStandard(...),
}
```

`AgentStandard` is a frozen dataclass with `description`, `skills` (ordered tuples of `(skill_name, params_overlay)`), and optional metadata (`category`, `tags`, `output_artifact_kind`).

A generic primitive lives in `axiom/infra/standards.py`:

```python
register_standards(agent: str, standards: dict[str, AgentStandard]) -> None
get_standards(agent: str) -> dict[str, AgentStandard]
get_standard(agent: str, name: str) -> AgentStandard
execute_standard(agent: str, name: str, params: dict, ctx: SkillContext) -> StandardResult
```

`execute_standard` walks the skill sequence, threading params, collecting per-step receipts, and returning a `StandardResult` that mirrors the ADR-055 `ActionEnvelope` shape — one composed receipt per standard execution.

### Three surfaces, one registry

- **CLI:** `axi <agent> standards` lists; `axi <agent> do <name> [args]` executes.
- **A2A:** `a2a.invoke(target=agent, skill="standards", params={"category": "prd"})` returns the catalog. `a2a.invoke(target=agent, skill="do", params={"name": "publish_prd", ...})` runs one.
- **MCP:** the builtin MCP server auto-exposes `axiom_<agent>__standards` and `axiom_<agent>__do_<standard>` per ADR-056's "every skill is MCP-exposable" rule.

The CLI, A2A, and MCP surfaces read the *same* `register_standards` registry. There is one source of truth.

### Persona-driven shaping

The agent persona file (e.g. `agents/press/persona.md`) gains a `## Standards` section that lists the registered standards by name + one-line description. This is what the LLM-mediated agent shape reads when an operator says *"PRESS, do this the standard way"* in chat — the persona consults the registry to know what "the standard way" is.

### Per-extension fit

The registry primitive is generic (`axiom/infra/standards.py`); each extension owns its own `standards.py` and calls `register_standards("press", STANDARDS)` at import time. There is no central catalog; discovery is by walking registered agents.

## Consequences

### Positive

- **Operator intent becomes addressable.** "Do this the standard way" resolves to a named bundle the operator (or any harness) can invoke.
- **Peer-agent composition is declarative.** RIVET can ask PRESS what its publishing standards are without importing PRESS code.
- **External harnesses get parity for free.** Claude Code / Cursor / ChatGPT Desktop see the same surface a local `axi` invocation does.
- **Standards are reviewable in code.** Adding a new convention means editing `standards.py`, not docs that drift from behavior.
- **The 8 RIVET skills + future TIDY skills + HERALD's recipient + PRESS's generate flow** all compose into a small set of operator-meaningful bundles (per-agent ≤ ~10 standards).
- **Composes with ADR-055 governance fabric.** A standard execution emits one composed `ActionEnvelope` for audit; sub-skill receipts thread under it.

### Negative

- **One more layer of indirection.** New extension authors learn the registry pattern, not just "write a skill." Mitigated: the registry is optional — extensions without standards still register skills the old way.
- **Versioning.** When a standard's skill sequence changes, its receipts thread differently. Resolved: each `AgentStandard` carries a `version: str` field; receipts capture the version at execution.
- **Naming discipline.** Operator-meaningful names ("publish_prd", "publish_for_review") are a UX surface that drifts under pressure. Resolved: a brief naming convention doc lives next to the primitive in `axiom/infra/standards.md`.

### Neutral

- **No CLI / MCP / A2A code changes are required up front.** The primitive ships as a library; surfaces hook in as M2-M4 milestones of the v0.30 plan land.

## How to use this list

When adding a new convention to an agent extension:

1. Implement the underlying skills per ADR-056 (`<ext>/skills/<name>.py`).
2. Declare a `<ext>/standards.py` with one or more `AgentStandard` entries naming the bundle.
3. Call `register_standards("<agent>", STANDARDS)` at module import.
4. Add a `## Standards` line per bundle in `<ext>/agents/<persona>/persona.md`.
5. The CLI / A2A / MCP surfaces pick it up at next process start; no platform-code change.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
