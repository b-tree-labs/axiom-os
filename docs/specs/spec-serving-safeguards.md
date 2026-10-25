# Tech Spec: Serving safeguards — the layer that bounds what any one call can cost

**Status:** Living • **Last updated:** 2026-10-05
**Related:** [ADR-157](../adrs/adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (the decision), [prd-gold-serving-safeguards](../prds/prd-gold-serving-safeguards.md) (the requirements and phases), [ADR-132](../adrs/adr-132-sensing-faults-detect-carry-refuse.md), [ADR-136](../adrs/adr-136-uncertainty-is-a-platform-primitive.md)

## 1) What is built

One pure module, `axiom/infra/serving_guard.py`, holds the policy and the
mechanisms; the verbs stay thin and the limits live at the doors. No database
import exists in the module (a test enforces this), so refusal is cheap by
construction.

| Mechanism | Where it acts | What it does |
|---|---|---|
| **Caller classes** | `serving_guard.caller_class` | `mcp`/`agent_tool` → `agent`; `cli`/`web`/`chat` → `interactive`; `runner` → `service`; anything unmapped → `anonymous` (smallest budget, on purpose). The surface is stamped onto `SkillContext.surface` by `invoke_capability` — never read from params. |
| **Cost classes** | `SkillSpec.cost_class` | `lookup` / `aggregate` / `series` / `scan`, declared where the specs are built (`data_platform/skills/__init__.py`). A serving verb without one is refused at its first call. |
| **Policy table** | `ServingPolicy` + `load_policy()` | Shipped defaults (PRD §9.1) overlaid by `~/.axi/config/serving_policy.toml` (`$AXIOM_CONFIG_DIR` honoured). A file can tune numbers and suspend principals; it cannot remove a bound — there is no "unlimited" spelling. |
| **Admission** | `ServingGuard.admit`, called in `data_platform/skills/gold.py::_run` | In order: suspension, declared cost class, per-principal token bucket, per-principal concurrency. Each refusal is typed and metered. |
| **Preflight** | `preflight_series` in the series verb's `pre` hook | Estimates points from window ÷ bucket BEFORE a connection opens. No window → refusal naming the cheaper call. Over budget → the bucket is widened across the whole window (reshape), never the oldest slice. |
| **Backstop** | `sample_evenly` in `_bound_series` | What an estimate cannot see (grouped series multiply points by groups) is sampled evenly after the query, both ends kept. |
| **Served block** | `served_block`; leads the envelope | `requested` / `returned_points` / `resolution_seconds` / `covered` / `reduced` first, so a consumer that stops reading early has already seen them. |
| **Byte budget (MCP)** | `mcp/skill_tools.py::_make_handler` | Every registry-tool result is bounded to the agent-class byte budget for its cost class via `bound_bytes` (largest list sampled evenly; note appended). Undeclared arguments are refused, not forwarded — closing the `dsn=` injection. |
| **Deployment-owned DSN** | `gold.py::_run` calls `resolve_dsn()` with no params | A request cannot redirect the database. The environment and platform config remain the only sources. |
| **Chat turn budgets** | `chat/agent.py` | `MAX_TOOL_CALLS_PER_ROUND = 8` (excess calls answered with a typed error, not executed) and `MAX_TOOL_RESULT_BYTES = 64 KB` via `_bounded_result_json` (the trimmer keeps the newest message however large, so the bound sits before the message exists). |
| **Metering + suspend** | `ServingGuard.metering()`, `data.serving_report`, `suspended_principals` in the policy file | Per-principal admitted/refused counts since process start; suspension is a config edit, no deploy. `axi data serving-report` / MCP `axiom_data__serving_report` reads it. |
| **Required index prefix** | `[tables."<name>"] required_filters` + `missing_required_filters` | A declared column the request never constrains (filter, grouping or verb-supplied) is a cheap typed refusal naming the cheap shape — measured about 3,000x between the two plans. Word-boundary match, so "website" is not "site". |
| **Rollup routing** | `[rollups."<name>"] table / native_bucket` + `choose_rollup` | An ask at least as coarse as the declared rollup reads the rollup; the served block names `source_table`. Reshape runs first, so a widened bucket can newly qualify. Undeclared → nothing changes. |

## 2) The policy file

```toml
# ~/.axi/config/serving_policy.toml — every entry optional; omitted entries
# keep shipped defaults. Values are policy; absence of a bound is impossible.
[budgets.agent.series]
default_points = 200
max_points = 2000
max_bytes = 256000

[limits.anonymous]
rate_per_second = 0.2
burst = 3
max_concurrent = 1

suspended_principals = ["@revoked-key"]

[tables."gold.signals"]
required_filters = ["site"]

[rollups."gold.signals"]
table = "gold.signals_1h"
native_bucket = "1 hour"
```

Shipped defaults (PRD §9.1 proposals, to be tuned against measured use):
agent 200/2,000 points and 64–256 KB; interactive 1,000/10,000 and 0.5–2 MB;
service 5,000/50,000 and 4–16 MB; anonymous 50/200 and 16 KB. Rates: agent
1 r/s burst 10 conc 4; interactive 5/20/8; service 10/40/8; anonymous
0.2/3/1. `HARD_ROW_CEILING = 100_000` whatever the file says.

## 3) The battery (Phase 4)

`tests/infra/test_serving_guard.py` (the mechanisms, with a negative control
proving the guard can fail), `data_platform/tests/test_serving_guard_at_the_door.py`
(the PRD's abusive personas against the real door: unbounded range, hostile
microsecond bucket, flood, anonymous caller, suspended principal, DSN
injection), `mcp/tests/unit_tests/test_skill_tools_bounds.py`,
`chat/tests/test_turn_budgets.py`, `data_platform/tests/test_compare_window.py`
(the window a loader dropped). All run in the unit suite, which is the release
gate's first job.

## 4) Deployment-side settings (owned by the consumer deployment)

Role-level database limits (statement, lock and idle-in-transaction timeouts,
`temp_file_limit`, connection limits) and front-door rate limits are
deployment configuration: they live in the consumer deployment's site
repository with their own apply/verify SQL and a deploy-time check, per the
PRD's Phase 0. This spec notes them so the division is explicit: the platform
bounds the call, the deployment bounds the substrate.

## 5) Deferred, with reasons

- **A serving database role split out of the owner role** (PRD Phase 2.4):
  the owner role runs migrations, conformance and the nightly backup, whose
  healthy pass holds a six-hour transaction; splitting it is an install-path
  change, not a timeout edit.
- **Compression of the largest raw table** (Phase 3.2): explicitly sequenced
  with the storage work already planned on the node; a production operation,
  not a code change here.
- **The consumer pack's freshness probe** (Phase 0.5): the full-history scan
  reached by an unauthenticated readiness endpoint is consumer code; its fix
  lands in the consumer repository.
- **Cross-process quotas**: the guard is per process, exactly as the
  connections it protects are; a shared budget across processes needs a store
  and is not worth one yet.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
