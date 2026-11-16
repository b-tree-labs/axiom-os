# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Progressive rollout: a stage plan that advances itself while its gates hold (LOOP-F18).

A plan, per flag and environment::

    {"stages": [{"percentage": 1}, {"percentage": 5}, {"percentage": 25}, {"percentage": 100}],
     "soak": "1h",                       # the minimum time in each stage (a stage may set its own)
     "value": "on",                      # or "split": [{variant, weight}, ...] (LOOP-F17)
     "bucket_by": "site",                # the unit a percentage moves (LOOP-F14); default the flag's
     "gates": [{"kind": "incidents"},
               {"kind": "metric", "capability": "data.latest", "params": {...},
                "path": "data.rows.0.value", "op": "lte", "value": 0.05}],
     "on_fail": "halt"}                  # or "rollback"

Stages are percentages, rings of cohorts (``{"cohort": "internal"}``, cumulative), or
``{"everyone": true}``. The current stage stands for rollout rules
(:func:`.targeting.stage_rules`) the resolver evaluates after the flag's own rules.

The concepts are ADR-197's switch runner: each gate is a declared probe
(:class:`axiom.infra.switchplan.Probe`, kind ``callable``, run by
:func:`axiom.infra.switchplan.evaluate`), so **a gate that cannot be evaluated fails**;
a stage soaks before the next commit; a failure halts or rolls back by itself. The
platform scheduler runs :func:`advance_all` (``flags.rollout_tick``, a hosted
``[[extension.schedule]]``), so there is no loop of our own. Every automatic advance,
halt or roll-back writes one audit record and raises one notification: an advance is
announced; a halt is an incident (ADR-249), owned by RIVET.

Gate kinds are pluggable (:func:`register_gate_kind`). Built in:

* ``incidents``: open incidents naming ``flag:<key>`` (optionally of declared ``kinds``)
  number at most ``max`` (default 0). A rollout's own halt incidents are not counted.
* ``metric``: a declared **read** capability (a gold read, a telemetry read) is invoked
  through the dispatch chokepoint and the number at ``path`` compared with ``op`` /
  ``value``. A capability that writes is refused when the plan is saved.
* ``feedback``: feedback reports (LOOP-B) are not built yet; the kind is the seam, and a
  plan naming it is refused until a source registers it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from . import targeting as t

log = logging.getLogger(__name__)

MAX_STAGES = 20
ON_FAIL = ("halt", "rollback")
DEFAULT_SOAK = "1h"
_OPS = {"lte": lambda a, b: a <= b, "lt": lambda a, b: a < b, "gte": lambda a, b: a >= b,
        "gt": lambda a, b: a > b, "eq": lambda a, b: a == b}
ACTOR = "@rivet"
RECIPIENT = "@operator"

GateFn = Callable[[Mapping[str, Any], str, str, datetime], "tuple[bool, str]"]


# ------------------------------------------------------------------ gates


def _subject(key: str) -> str:
    return f"flag:{key}"


def _incidents_gate(gate: Mapping[str, Any], key: str, env: str, now: datetime) -> tuple[bool, str]:
    from axiom.extensions.builtins.notifications import incidents
    from axiom.extensions.builtins.notifications.incidents.stage import OPEN_STATES

    kinds = set(gate.get("kinds") or ())
    found = [i for i in incidents.default_store().in_states(OPEN_STATES)
             if _subject(key) in (i.subjects or []) and i.area != "features"
             and (not kinds or i.kind in kinds)]
    limit = int(gate.get("max", 0))
    detail = f"incidents: {len(found)} open naming {_subject(key)} (at most {limit})"
    if found:
        detail += ": " + "; ".join(f"{i.id} {i.summary}" for i in found[:3])
    return len(found) <= limit, detail


_registry_override: Any = None


def set_skill_registry(registry: Any) -> None:
    """Pin the registry a metric gate reads through (tests), or ``None`` for the default."""
    global _registry_override
    _registry_override = registry


def _skill_registry() -> Any:
    if _registry_override is not None:
        return _registry_override
    from axiom.infra.skills import default_registry

    return default_registry()


def _at_path(value: Any, path: str) -> Any:
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, Mapping):
            value = value[part]
        else:
            raise KeyError(part)
    if isinstance(value, Mapping) and "value" in value:
        value = value["value"]
    return value


def _metric_gate(gate: Mapping[str, Any], key: str, env: str, now: datetime) -> tuple[bool, str]:
    import logging as _logging

    from axiom.infra.skill_dispatch import invoke_capability
    from axiom.infra.skills import SkillContext

    reg = _skill_registry()
    cap = str(gate["capability"])
    ctx = SkillContext(registry=reg, state_dir=_STATE.get("dir"), logger=_logging.getLogger("axi.features"),
                       actor=ACTOR)
    result = invoke_capability(reg, cap, dict(gate.get("params") or {}), ctx, surface="agent_tool")
    if not result.ok:
        raise RuntimeError(f"could not read {cap}: {'; '.join(result.errors) or 'it failed'}")
    got = _at_path(result.value, str(gate.get("path") or ""))
    if isinstance(got, bool) or not isinstance(got, int | float):
        raise RuntimeError(f"{cap} at {gate.get('path')!r} is {got!r}, not a number")
    ok = _OPS[str(gate["op"])](float(got), float(gate["value"]))
    return ok, f"metric {cap} {gate.get('path')} = {got} ({gate['op']} {gate['value']} {'holds' if ok else 'fails'})"


_BUILTIN_GATES: dict[str, GateFn] = {"incidents": _incidents_gate, "metric": _metric_gate}
_gates: dict[str, GateFn] = dict(_BUILTIN_GATES)
#: The state dir a tick runs under, for the reads a metric gate makes.
_STATE: dict[str, Any] = {}


def register_gate_kind(kind: str, fn: GateFn) -> None:
    """Add a gate kind: ``fn(gate, flag_key, environment, now) -> (ok, detail)``. Raising fails the gate."""
    _gates[kind] = fn


def reset_gate_kinds() -> None:
    _gates.clear()
    _gates.update(_BUILTIN_GATES)


def evaluate_gates(gates: Iterable[Mapping[str, Any]], key: str, env: str, now: datetime) -> list[Any]:
    """Every gate as an ADR-197 probe: a gate that cannot be evaluated fails, with the reason."""
    from axiom.infra.switchplan import Probe, evaluate

    results = []
    for i, gate in enumerate(gates):
        kind = str(gate.get("kind") or "")
        fn = _gates.get(kind)

        def run(_ctx: Any, fn: GateFn | None = fn, gate: Mapping[str, Any] = gate, kind: str = kind) -> Any:
            if fn is None:
                raise LookupError(f"no gate kind {kind!r} is registered here")
            return fn(gate, key, env, now)

        results.append(evaluate(Probe(name=f"{kind or 'gate'}#{i}", kind="callable", when="post", fn=run)))
    return results


# ------------------------------------------------------------------ plans


def _check_gate(i: int, gate: Any) -> dict[str, Any]:
    if not isinstance(gate, Mapping) or not gate.get("kind"):
        raise t.RuleError(f"gate {i} is {{kind, ...}}.")
    kind = str(gate["kind"])
    if kind == "feedback" and kind not in _gates:
        raise t.RuleError(
            f"gate {i}: a feedback gate reads feedback reports (LOOP-B), which no source provides here yet; "
            "a source registers the feedback kind (progressive.register_gate_kind)."
        )
    if kind not in _gates:
        raise t.RuleError(f"gate {i}: no gate kind {kind!r}; the kinds here: {', '.join(sorted(_gates))}.")
    if kind == "metric":
        cap = str(gate.get("capability") or "")
        spec = _skill_registry().spec(cap) if cap and _skill_registry().has(cap) else None
        if spec is None:
            raise t.RuleError(f"gate {i}: the metric capability {cap!r} is not registered here.")
        if spec.effective_risk != "read":
            raise t.RuleError(f"gate {i}: {cap} is not a read ({spec.effective_risk}); a gate only reads.")
        if gate.get("op") not in _OPS:
            raise t.RuleError(f"gate {i}: op is one of {', '.join(_OPS)}.")
        v = gate.get("value")
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise t.RuleError(f"gate {i}: value is the number the metric is compared with.")
    if kind == "incidents" and not isinstance(gate.get("max", 0), int):
        raise t.RuleError(f"gate {i}: max is a whole number.")
    return dict(gate)


def check_plan(plan: Any, *, flag: Any, known_cohorts: Iterable[str], known_attributes: Iterable[str]) -> dict[str, Any]:
    """The plan, validated and normalised, or :class:`.targeting.RuleError`."""
    if not isinstance(plan, Mapping):
        raise t.RuleError("a stage plan is {stages, soak, value | split, gates, on_fail}.")
    extra = set(plan) - {"stages", "soak", "value", "split", "bucket_by", "gates", "on_fail", "note"}
    if extra:
        raise t.RuleError(f"unexpected keys in the stage plan: {sorted(extra)}.")
    cohorts = set(known_cohorts)
    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > MAX_STAGES:
        raise t.RuleError(f"stages is a list of 1 to {MAX_STAGES} stages.")
    out_stages: list[dict[str, Any]] = []
    last_pct = 0.0
    for i, st in enumerate(stages):
        if not isinstance(st, Mapping):
            raise t.RuleError(f"stage {i} is an object.")
        kinds = [k for k in ("percentage", "cohort", "everyone") if k in st]
        if len(kinds) != 1 or set(st) - {"percentage", "cohort", "everyone", "soak"}:
            raise t.RuleError(f"stage {i} is one of {{percentage}}, {{cohort}} or {{everyone: true}}, with an optional soak.")
        norm: dict[str, Any] = {}
        if "percentage" in st:
            pct = st["percentage"]
            if isinstance(pct, bool) or not isinstance(pct, int | float) or not 0 < pct <= 100 or pct <= last_pct:
                raise t.RuleError(f"stage {i}: percentages rise, each above 0 and at most 100.")
            last_pct, norm["percentage"] = float(pct), pct
        elif "cohort" in st:
            if st["cohort"] not in cohorts:
                raise t.RuleError(f"stage {i} names unknown cohort {st['cohort']!r}.")
            norm["cohort"] = st["cohort"]
        else:
            if st["everyone"] is not True:
                raise t.RuleError(f"stage {i}: everyone is true.")
            norm["everyone"] = True
        if "soak" in st:
            norm["soak_s"] = t.parse_duration(st["soak"], f"stage {i} soak")
        out_stages.append(norm)
    out: dict[str, Any] = {"stages": out_stages,
                           "soak_s": t.parse_duration(plan.get("soak", DEFAULT_SOAK), "soak")}
    normalize = flag.normalize if flag is not None else t.normalize_value
    if plan.get("split") is not None:
        if "value" in plan:
            raise t.RuleError("a stage plan serves a value or a split, not both.")
        out["split"] = t.validate_split(plan["split"], normalize, "the stage plan")
    else:
        value = normalize(plan.get("value", "on" if flag is None or not flag.multivariate else None))
        if value is None:
            raise t.RuleError("the stage plan's value is one the flag can take.")
        out["value"] = value
    unit = t.check_unit(plan.get("bucket_by"), known_attributes=known_attributes, known_cohorts=cohorts,
                        where="the stage plan's bucket_by")
    if unit:
        out["bucket_by"] = unit
    gates = plan.get("gates") or []
    if not isinstance(gates, list):
        raise t.RuleError("gates is a list.")
    out["gates"] = [_check_gate(i, g) for i, g in enumerate(gates)]
    on_fail = str(plan.get("on_fail") or "halt")
    if on_fail not in ON_FAIL:
        raise t.RuleError(f"on_fail is {' or '.join(ON_FAIL)}.")
    out["on_fail"] = on_fail
    if plan.get("note"):
        out["note"] = str(plan["note"])
    return out


def soak_for(plan: Mapping[str, Any], index: int) -> int:
    stages = plan.get("stages") or []
    if 0 <= index < len(stages) and "soak_s" in stages[index]:
        return int(stages[index]["soak_s"])
    return int(plan.get("soak_s", 3600))


def stage_label(plan: Mapping[str, Any], index: int) -> str:
    stages = plan.get("stages") or []
    if not 0 <= index < len(stages):
        return "nothing (rolled back)"
    st = stages[index]
    if "percentage" in st:
        return f"{st['percentage']}%"
    return "everyone" if st.get("everyone") else f"cohort {st['cohort']}"


# ------------------------------------------------------------------ the tick


def _aware(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def _send(summary: str, body: str, *, cause: Any = None, intent: str) -> None:
    import importlib

    from axiom.extensions.builtins.notifications.skills.send import _ctx
    from axiom.governance import Classification

    notify = importlib.import_module("axiom.extensions.builtins.notifications.send")
    notify.send(_ctx(), actor=ACTOR, recipient=RECIPIENT,
                payload=notify.NotificationPayload(summary=summary, body=body),
                classification=Classification.INTERNAL,
                priority=notify.Priority.HIGH if cause is not None else notify.Priority.NORMAL,
                intent=intent, cause=cause)


def _notify(event: str, row: Mapping[str, Any], to: int, why: str) -> None:
    """One notification per automatic transition; never raises."""
    key, env, plan = row["key"], row["environment"], row["plan"]
    where = f"{key} ({env})"
    try:
        if event in ("advanced", "done"):
            summary = (f"The rollout of {where} moved to stage {to + 1} of {len(plan['stages'])}: "
                       f"{stage_label(plan, to)}." if event == "advanced"
                       else f"The rollout of {where} finished: {stage_label(plan, to)}, its gates held.")
            _send(summary, why, intent="features.stage_advance")
            return
        from axiom.extensions.builtins.notifications.incidents.model import Cause, Unmet

        cli = _cli()
        cause = Cause(
            area="features", kind="rollout_halted", action="flags.rollout_tick", subject=_subject(key),
            error_class="GateFailed",
            unmet=(Unmet(id="stage-gates-hold", why=why,
                         fix=f"look with `{cli} flags explain {key}`; resume with `{cli} flags stage set "
                             f"{key} --stage {max(row['stage'], 0)} --apply`"),),
            detail={"environment": env, "event": event, "stage": row["stage"]},
        )
        summary = (f"The rollout of {where} rolled back: a gate failed." if event == "rolled_back"
                   else f"The rollout of {where} halted at {stage_label(plan, row['stage'])}: a gate failed.")
        _send(summary, why, cause=cause, intent="features.stage_halt")
    except Exception as exc:  # noqa: BLE001 - a transition is never undone by the alert path
        log.warning("stage plan notification for %s not sent: %s", where, type(exc).__name__)


def _audit(ctx: Any, skill: str, row: Mapping[str, Any], new: Mapping[str, Any]) -> None:
    from axiom.infra import audit_trail

    if ctx is None or getattr(ctx, "state_dir", None) is None:
        return
    audit_trail.emit(ctx, skill=skill, params={"key": row["key"], "environment": row["environment"]},
                     outcome="ok", errors_count=0,
                     change={"kind": "stage-plan", "subject": f"{row['key']} ({row['environment']})",
                             "old": {"stage": row["stage"], "status": row["status"]}, "new": dict(new)})


def advance_all(store: Any, *, now: datetime | None = None, ctx: Any = None) -> dict[str, list[str]]:
    """Advance, finish, halt or roll back every running plan, once (one scheduler tick)."""
    now = now or datetime.now(UTC)
    _STATE["dir"] = getattr(ctx, "state_dir", None)
    out: dict[str, list[str]] = {"advanced": [], "done": [], "halted": [], "rolled_back": []}
    for row in store.all_stage_plans():
        if row["status"] != "running":
            continue
        key, env, plan, index = row["key"], row["environment"], row["plan"], row["stage"]
        where = f"{key} ({env})"
        results = evaluate_gates(plan.get("gates") or [], key, env, now)
        failed = [r for r in results if not r.ok]
        if failed:
            why = "; ".join(r.detail or r.name for r in failed)
            rollback = plan.get("on_fail") == "rollback"
            status, to = ("rolled_back", -1) if rollback else ("halted", index)
            store.move_stage_plan(key, env, status=status, stage=to, by=ACTOR, why=why, now=now)
            _audit(ctx, "features.stage_rollback" if rollback else "features.stage_halt", row,
                   {"stage": to, "status": status, "why": why})
            _notify(status, row, to, why)
            out[status].append(where)
            continue
        if (now - _aware(row["stage_entered_at"])).total_seconds() < soak_for(plan, index):
            continue
        held = "; ".join(r.detail for r in results if r.detail) or "no gates declared"
        last = len(plan.get("stages") or []) - 1
        if index >= last:
            store.move_stage_plan(key, env, status="done", stage=index, by=ACTOR, why=held, now=now)
            _audit(ctx, "features.stage_advance", row, {"stage": index, "status": "done"})
            _notify("done", row, index, held)
            out["done"].append(where)
            continue
        store.move_stage_plan(key, env, status="running", stage=index + 1, by=ACTOR, why=held, now=now)
        _audit(ctx, "features.stage_advance", row, {"stage": index + 1, "status": "running"})
        _notify("advanced", row, index + 1, held)
        out["advanced"].append(where)
    return out


__all__ = [
    "ACTOR",
    "advance_all",
    "check_plan",
    "evaluate_gates",
    "register_gate_kind",
    "reset_gate_kinds",
    "set_skill_registry",
    "soak_for",
    "stage_label",
]
