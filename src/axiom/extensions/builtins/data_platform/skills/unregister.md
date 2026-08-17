# SKILL: data.unregister

**Owner:** `axi data unregister` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active
**Last updated:** 2026-05-30

## What this skill does

See `unregister.py` for the function body. CLI verb `axi data unregister` is a thin
wrapper that translates flags → params dict and calls the registered
skill function. The same skill is reachable from any agent persona
through `ctx.registry.invoke("data.unregister", params, ctx)`.

## Inputs / Outputs

See the function docstring at `axiom.extensions.builtins.data_platform.skills.unregisterun`.
Returns a uniform `SkillResult` ({ok, value, errors, actions_taken}).

## Safety

External mutations route through `agent_action_guard.guarded_act`
per ADR-045 D6 where applicable (notably `data.ingest`'s per-item
writes). `data.install` is reversible (`helm uninstall` undoes it);
`data.diagnose` and `data.troubleshoot` are read-only.
