# ADR-056 — Skills as invocable functions (bidirectional A2A)

**Status:** Accepted · **Date:** 2026-05-30
**Builds on:** ADR-031 (extension self-containment), [spec-aeos-0.1.md §4.6 skill]
**Related:** ADR-045 (RACI v2 D6 / guarded_act), ADR-049 (orchestration boundary)

## Context

Today an AEOS `skill` is a SKILL.md directory — manifest entry, markdown
body, optional scripts. The `Skill` dataclass at
`src/axiom/extensions/contracts.py` carries `name + path + description`
only. Skills are **discoverable but not invocable**: every agent's
operations are implemented inside its own CLI handlers, so the only way
to invoke a skill is to shell out to that CLI.

That's wrong for two reasons:

1. **No agent-to-agent reach.** When PLINTH's `troubleshoot-install`
   reasons about a failed deploy it needs to call other agents' surfaces
   (capabilities probe, log tail, memory recall, RAG retrieve, user
   prompt). Shelling out to `axi capabilities probe` from inside a
   Python skill function is grotesque, slow, and loses context.

2. **CLI is the only entrypoint.** Another agent that wants to do what
   `axi plinth ingest` does cannot reuse the implementation —
   it has to re-shell or duplicate. AEOS's skill discovery has no
   runtime invocation contract.

## Layering — keep these straight

| Layer | What it is | Determinism |
|---|---|---|
| **Agent persona** (TIDY, PLINTH, AXI, RIVET, CHALKE, …) | LLM character with personality; used for reasoning over irregular situations | non-deterministic |
| **CLI noun + verbs** (`axi data`, `axi hygiene`, …) | The deterministic platform "arms and legs" — purpose-named, no LLM | deterministic |
| **Skill** | A Python function with schema, registered once, invoked by either layer above | deterministic by default; may itself call into an LLM persona |

The CLI verb invokes the skill. The agent persona invokes the *same*
skill. The skill is the executable unit; the agent persona's role is to
reason about *when* and *why*, not to re-implement the operation.

## Decision

**Skills are first-class invocable functions, registered through a
process-local `SkillRegistry`, callable by any agent or CLI verb.**

### Skill function shape

```python
def my_skill(
    params: dict[str, Any],
    ctx: SkillContext,
) -> SkillResult: ...
```

`params` is the parsed input (typed against the skill's schema). `ctx`
is the runtime handle:

```python
@dataclass(frozen=True)
class SkillContext:
    registry: SkillRegistry      # for skill.invoke() — bidirectional A2A
    state_dir: Path              # `$AXI_STATE/`
    logger: logging.Logger
    user_prompt: Callable[[str], str] | None   # interactive escalation
```

`SkillResult` is a dataclass with `ok: bool`, `value`, `errors`,
`actions_taken` — uniform across skills so callers handle outcomes the
same way.

### Registration

Extensions register skills at import time, OR declare them in
`axiom-extension.toml` and the discovery loader binds them. Manifest
entry adds `entry = "module.path:function_name"`:

```toml
[[extension.provides]]
kind = "skill"
name = "install"                                # qualified as `data.install`
path = "agents/plinth/skills/install.md"        # AEOS markdown (existing)
entry = "axiom.extensions.builtins.data_platform.skills.install:run"
schema = "agents/plinth/skills/install.schema.json"   # optional JSON schema
```

The registry namespaces skills under their extension's `noun`
(`data.install`, `hygiene.prune`, `release.tag`). Cross-extension
invocation is just `registry.invoke("hygiene.prune", params, ctx)`.

### Bidirectional A2A

Inside any skill, the context exposes the registry. The skill can
invoke other skills with **no extra ceremony**:

```python
def troubleshoot_install(params, ctx):
    caps = ctx.registry.invoke("capabilities.probe", {"binaries": ["helm", "kubectl"]}, ctx)
    if not caps.ok:
        logs = ctx.registry.invoke("log.tail", {"unit": "dagster-daemon", "lines": 100}, ctx)
        ...
```

CLI verbs become **thin wrappers**:

```python
def _cmd_install(args):
    return registry.invoke("data.install", _args_to_params(args), build_ctx()).exit_code
```

### Safety

Skills that mutate external state route their write through
`agent_action_guard.guarded_act` per ADR-045. The registry doesn't
re-guard — that's the skill's responsibility, declared in the manifest
(`provides.mutates = true`).

### What this is NOT

- **Not** an RPC layer. Same-process Python invocation only. Cross-
  process A2A (PLINTH in a pod, AXI in a CLI) is a future ADR; the
  registry's `invoke` signature is RPC-compatible so the upgrade is
  additive.
- **Not** a replacement for SKILL.md. The markdown body is still the
  human-readable spec; the function is the executable.
- **Not** a service mesh. Skills are reentrant, stateless, and own
  their persistence through `axiom.infra.db.session_for("<ext>")` per
  ADR-051.

## Consequences

**+** Every agent's surface becomes reusable from every other agent.
PLINTH can call AXI's capabilities; TIDY can call RIVET's stale-branch
list; any agent can call PLINTH's `data.install` directly without a
subprocess.

**+** CLI verbs collapse to thin wrappers. The CLI argparse layer maps
flags → params dict, calls `registry.invoke`, prints the result. No
business logic in CLI handlers.

**+** Skills become unit-testable as pure functions with a stub ctx.

**−** Existing CLI handlers must be refactored to extract their logic
into skill functions. Doable incrementally per extension; the verb-
grammar audit migration is the natural moment to do both at once.

**−** Schema validation adds a small surface (JSON schema files
alongside SKILL.md). Worth it for runtime safety + LLM-callability.

## Implementation

Lands in this PR:

- `axiom/infra/skills.py` — `SkillRegistry`, `SkillContext`, `SkillResult`.
- AEOS manifest schema extension (optional `entry` + `schema` fields
  on `kind="skill"` blocks).
- Reference implementation: PLINTH's new skills (`data.install`,
  `data.diagnose`, `data.troubleshoot`, `data.ingest`, `data.register`)
  all registered through the new registry; CLI verbs become wrappers.

Follow-up PRs (one per agent): hygiene/release/triage skills migrated
to the same pattern as the verb-grammar audit lands.
