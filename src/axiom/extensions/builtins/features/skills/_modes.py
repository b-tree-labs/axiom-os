# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every standard rollout mode: the verbs beyond phase 2 (ADR-257 D5, LOOP-F16 to F19, F23).

* ``flags.cohort_import_preview`` / ``flags.cohort_import``: a large list, in bulk (F16).
* ``flags.cohort_refresh``: take a following cohort's members from its source now (F16).
* ``flags.stage_plan_preview`` / ``flags.stage_plan_set``: a progressive rollout (F18).
* ``flags.stage_plan_halt``: stop (or roll back) a plan at once; low permission, like
  the kill switch, so a person can stop a rollout fast.
* ``flags.approve``: a second person approves a held change (F23); offered only at the
  command line, never to an AI client.
* ``flags.changes`` / ``flags.change_cancel``: what waits on approval or on its time (F19).
* ``flags.rollout_tick``: what the platform scheduler runs every minute: apply the
  scheduled changes that are due, advance or halt the stage plans, refresh the cohorts
  that follow a source.

Writes go through :func:`._loop.write`, so a stage plan or an import is approved and
scheduled exactly like a rollout or a cohort.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.audit_trail import note_change
from axiom.infra.skills import SkillContext, SkillResult

from ._flags import _service, this_node
from ._loop import (
    SAMPLE,
    _actor,
    _cohort_view,
    _declared,
    _env,
    _fail,
    _json,
    _label,
    _list,
    _Overlay,
    _population,
    _Refused,
    _schedule,
    _stored_params,
    _targeting,
    _when,
    apply_cohort_delete,
    apply_cohort_set,
    apply_rollout_set,
    cohort_policies,
    write,
)

# ------------------------------------------------------------------ bulk import (LOOP-F16)


def _members(params: Mapping[str, Any]) -> list[str]:
    from .. import cohort_sources as cs

    if params.get("file") not in (None, ""):
        path = Path(str(params["file"])).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _Refused(f"cannot read {path}: {exc.strerror or exc}") from exc
        try:
            members = cs.parse_members(text, name=path.name)
        except cs.SourceError as exc:
            raise _Refused(str(exc)) from exc
    elif params.get("members") is not None:
        members = _list(_json(params["members"], "members"))
    else:
        raise _Refused("give the members: a file, or a list in members")
    if len(members) > cs.MAX_MEMBERS:
        raise _Refused(f"{len(members)} members; a cohort holds at most {cs.MAX_MEMBERS}")
    return members


def distinct(values: list[str]) -> list[str]:
    """In first-seen order, linear in the list."""
    seen: set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


def _plan_import(params: Mapping[str, Any]) -> tuple[Any, Any, str, dict[str, Any], dict | None, dict[str, Any]]:
    svc = _service()
    ts = _targeting(svc)
    key = str(params.get("key") or "").strip()
    field_name = {"principal": "principals", "account": "accounts"}.get(str(params.get("kind") or "principal"))
    if field_name is None:
        raise _Refused("kind is principal or account")
    mode = str(params.get("mode") or "replace")
    if mode not in ("replace", "add"):
        raise _Refused("mode is replace or add")
    incoming = _members(params)
    current = ts.cohort(key)
    if current and current.get("source"):
        raise _Refused(f"cohort {key!r} follows a source; its members come from there")
    old = list((current or {}).get(field_name) or [])
    new = distinct(old + incoming) if mode == "add" else distinct(incoming)
    after = {"name": (current or {}).get("name") or "", "description": (current or {}).get("description") or "",
             "rules": (current or {}).get("rules"), "principals": list((current or {}).get("principals") or []),
             "accounts": list((current or {}).get("accounts") or [])}
    after[field_name] = new
    oldset, newset = set(old), set(new)
    diff = {"added": len(newset - oldset), "removed": len(oldset - newset), "field": field_name}
    try:
        from .. import targeting as t

        ts.check_cohort(key, rules=after["rules"])
    except t.RuleError as exc:
        raise _Refused(str(exc)) from exc
    return svc, ts, key, after, _cohort_view(current), diff


def _import_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """The call as approvals and schedules bind it: the members themselves, never a file path
    whose content could change between the approval and the write."""
    import hashlib

    members = _members(params)
    out = {k: v for k, v in _stored_params(params).items() if k not in ("file", "members")}
    out["members"] = members
    out["members_sha256"] = hashlib.sha256("\n".join(members).encode()).hexdigest()
    return out


def cohort_import_preview(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohort_import_preview``: what an import would list, added and removed. Writes nothing."""
    try:
        _, _, key, after, before, diff = _plan_import(params)
        run_at, tz = _schedule(params)
    except (_Refused, ValueError) as exc:
        return _fail(exc)
    size = len(after["principals"]) + len(after["accounts"])
    return SkillResult(value={"key": key, "applied": False, "before": before,
                              "after": _cohort_view({**after, "size": size}), **diff,
                              "scheduled_for": _when(run_at, tz)})


def cohort_import(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohort_import``: replace (default) or add to a cohort's list in bulk (destructive)."""
    if not str(params.get("reason") or "").strip():
        return SkillResult(ok=False, errors=["give a reason: every cohort write records why"])
    try:
        svc, ts, key, _, _, _ = _plan_import(params)
        bound = _import_params(params)
        return write("features.cohort_import", bound, ctx, ts=ts, policies=cohort_policies(svc, ts, key),
                     subject=f"cohort {key}", environment=None, apply=lambda by: apply_cohort_import(bound, by))
    except (_Refused, ValueError) as exc:
        return _fail(exc)


def apply_cohort_import(params: Mapping[str, Any], by: str) -> SkillResult:
    _, ts, key, after, before, diff = _plan_import(params)
    row = ts.put_cohort(key, by=by, reason=str(params.get("reason") or ""), validate=False, **after)
    view = _cohort_view(row)
    note_change(kind="cohort", subject=key, old=before, new=view)
    return SkillResult(
        value={"key": key, "applied": True, "before": before, "after": view, **diff, "updated_by": by},
        actions_taken=[f"imported cohort {key}: {row['size']} listed (+{diff['added']}, -{diff['removed']})"],
    )


def cohort_refresh(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohort_refresh``: take a following cohort's members from its source now."""
    try:
        ts = _targeting(_service())
    except _Refused as exc:
        return _fail(exc)
    key = str(params.get("key") or "").strip()
    out = refresh_one(ts, key)
    if out is None:
        return SkillResult(ok=False, errors=[f"no cohort {key!r} follows a source"])
    if out.get("error"):
        return SkillResult(ok=False, errors=[f"cohort {key}: the source could not be read, so its "
                                             f"{out['size']} members are kept: {out['error']}"], value=out)
    note_change(kind="cohort", subject=key, old={"size": out["before"]}, new={"size": out["size"]})
    return SkillResult(value=out, actions_taken=[f"refreshed cohort {key} from its source: {out['size']} members"])


def refresh_one(ts: Any, key: str, now: datetime | None = None) -> dict[str, Any] | None:
    from .. import cohort_sources as cs

    cohort = ts.cohort(key)
    if not cohort or not cohort.get("source"):
        return None
    before = cohort["size"]
    try:
        members = cs.fetch(cohort["source"])
    except cs.SourceError as exc:
        row = ts.refreshed(key, error=str(exc), at=now)
        return {"key": key, "before": before, "size": row["size"], "error": str(exc)}
    row = ts.refreshed(key, principals=members, at=now)
    return {"key": key, "before": before, "size": row["size"], "error": None}


# ------------------------------------------------------------------ stage plans (LOOP-F18)


def _plan_stage(params: Mapping[str, Any]) -> tuple[Any, Any, str, str, dict[str, Any], int, dict | None]:
    from .. import attributes, progressive
    from .. import targeting as t

    svc = _service()
    key = str(params.get("key") or "").strip()
    _declared(svc, key)
    ts = _targeting(svc)
    env = _env(params)
    raw = _json(params.get("plan"), "plan")
    try:
        plan = progressive.check_plan(raw, flag=svc.get_registry()[key], known_cohorts=set(ts.cohorts()),
                                      known_attributes=set(attributes.catalogue()))
    except t.RuleError as exc:
        raise _Refused(str(exc)) from exc
    stage = params.get("stage") or 0
    if isinstance(stage, str):
        stage = int(stage) if stage.strip().isdigit() else -1
    if not 0 <= stage < len(plan["stages"]):
        raise _Refused(f"stage is 0 to {len(plan['stages']) - 1}")
    return svc, ts, key, env, plan, stage, ts.stage_plan(key, env)


def stage_plan_preview(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.stage_plan_preview``: each stage and how many it admits. Writes nothing."""
    from .. import progressive
    from ..resolver import FlagService

    try:
        svc, ts, key, env, plan, start, before = _plan_stage(params)
        run_at, tz = _schedule(params)
    except (_Refused, ValueError) as exc:
        return _fail(exc)
    subjects, source = _population(svc, _list(params.get("subjects")))
    stages = []
    for i, st in enumerate(plan["stages"]):
        row = {"plan": plan, "stage": i, "status": "running"}
        after = FlagService(overrides=svc.overrides, targeting=_Overlay(ts, stage=(key, env, row)))
        admitted: list[str] = []
        for principal, account in subjects:
            out = after.explain(key, node=this_node(), user=principal, account=account)
            if out["decided_by"] == "rollout" and "stage" in (out.get("detail") or {}):
                admitted.append(_label((principal, account)))
        stages.append({"index": i, **{k: v for k, v in st.items() if k != "soak_s"},
                       "label": progressive.stage_label(plan, i), "soak_s": progressive.soak_for(plan, i),
                       "admitted": len(admitted), "sample": admitted[:SAMPLE]})
    return SkillResult(value={
        "key": key, "environment": env, "applied": False, "before": before, "after": plan, "start": start,
        "stages": stages, "gates": plan["gates"], "on_fail": plan["on_fail"],
        "population": {"size": len(subjects), "source": source}, "scheduled_for": _when(run_at, tz),
    })


def stage_plan_set(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.stage_plan_set``: create or replace a flag's stage plan (destructive); it starts now."""
    if not str(params.get("reason") or "").strip():
        return SkillResult(ok=False, errors=["give a reason: every stage plan records why"])
    try:
        from .. import approvals as ap

        svc, ts, key, env, plan, stage, before = _plan_stage(params)
        return write("features.stage_plan_set", params, ctx, ts=ts,
                     policies=ap.policies_for([svc.get_registry()[key]], env), subject=f"{key} ({env})",
                     environment=env, apply=lambda by: apply_stage_plan_set(_stored_params(params), by))
    except (_Refused, ValueError) as exc:
        return _fail(exc)


def _stage_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"plan": row["plan"], "stage": row["stage"], "status": row["status"]}


def apply_stage_plan_set(params: Mapping[str, Any], by: str) -> SkillResult:
    from .. import progressive

    _, ts, key, env, plan, stage, before = _plan_stage(params)
    row = ts.put_stage_plan(key, env, plan=plan, by=by, reason=str(params.get("reason") or ""), stage=stage)
    note_change(kind="stage-plan", subject=f"{key} ({env})", old=_stage_view(before), new=_stage_view(row))
    return SkillResult(
        value={"key": key, "environment": env, "applied": True, "before": _stage_view(before),
               "after": _stage_view(row), "updated_by": by},
        actions_taken=[f"started the {env} stage plan of {key} at stage {stage + 1} of {len(plan['stages'])}: "
                       f"{progressive.stage_label(plan, stage)}"],
    )


def stage_plan_halt(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.stage_plan_halt``: halt a stage plan where it stands, or roll it back. Immediate."""
    svc = _service()
    key = str(params.get("key") or "").strip()
    try:
        _declared(svc, key)
        ts = _targeting(svc)
    except _Refused as exc:
        return _fail(exc)
    env = _env(params)
    reason = str(params.get("reason") or "").strip()
    if not reason:
        return SkillResult(ok=False, errors=["give a reason: a halted rollout says why"])
    mode = str(params.get("mode") or "halt")
    if mode not in ("halt", "rollback"):
        return SkillResult(ok=False, errors=["mode is halt or rollback"])
    before = ts.stage_plan(key, env)
    if before is None:
        return SkillResult(ok=False, errors=[f"{key} has no stage plan in {env}"])
    status, stage = ("rolled_back", -1) if mode == "rollback" else ("halted", before["stage"])
    row = ts.move_stage_plan(key, env, status=status, stage=stage, by=_actor(ctx), why=reason)
    note_change(kind="stage-plan", subject=f"{key} ({env})", old=_stage_view(before), new=_stage_view(row))
    return SkillResult(value={"key": key, "environment": env, **(_stage_view(row) or {}), "reason": reason},
                       actions_taken=[f"{'rolled back' if mode == 'rollback' else 'halted'} the {env} stage plan "
                                      f"of {key}"])


# ------------------------------------------------------------------ approvals and waiting changes (LOOP-F19, F23)


def approve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.approve``: a person other than the author approves a held change."""
    from .. import approvals as ap
    from ..flag_registry import ApprovalPolicy

    try:
        ts = _targeting(_service())
    except _Refused as exc:
        return _fail(exc)
    change_id = str(params.get("id") or "").strip()
    change = ts.change(change_id) if change_id else None
    if change is None:
        return SkillResult(ok=False, errors=[f"no change {change_id!r}; `flags changes` lists them"])
    if change["status"] not in ("awaiting_approval", "approved"):
        return SkillResult(ok=False, errors=[f"change {change_id} is {change['status']}; nothing to approve"])
    approver = _actor(ctx)
    refusal = ap.person_refusal(approver, change["author"])
    if refusal:
        return SkillResult(ok=False, errors=[refusal])
    if any(ap._name(a["by"]) == ap._name(approver) for a in change["approvals"]):
        return SkillResult(ok=False, errors=[f"{approver} has already approved change {change_id}"])
    try:
        roles = ap.roles_of(approver)
    except Exception as exc:  # noqa: BLE001 - an unreadable directory grants nothing
        return SkillResult(ok=False, errors=[f"the approver's roles could not be read: {exc}"])
    policies = [ApprovalPolicy(approvals=p["approvals"], roles=tuple(p.get("roles") or ()), source=p.get("source", ""))
                for p in change["policy"]]
    from axiom.governance.roles import canonical_roles

    # Current ids on both sides: a policy naming ``operator`` means ``site-admin`` (ADR-261).
    if policies and not any(not p.roles or set(canonical_roles(roles)) & set(canonical_roles(p.roles))
                            for p in policies):
        wanted = sorted({r for p in policies for r in p.roles})
        return SkillResult(ok=False, errors=[f"{approver} holds none of the roles this change needs "
                                             f"({', '.join(wanted)}); they hold {', '.join(roles) or 'none'}"])
    approvals = [*change["approvals"], {"by": approver, "roles": list(roles), "at": datetime.now(UTC).isoformat(),
                                        "note": str(params.get("reason") or "").strip() or None}]
    missing = ap.outstanding(policies, approvals, change["author"])
    row = ts.update_change(change_id, approvals=approvals, status="approved" if not missing else change["status"])
    note_change(kind="approval", subject=change["subject"], old=None,
                new={"change": change_id, "by": approver, "outstanding": missing})
    return SkillResult(
        value={"change": row, "outstanding": missing},
        actions_taken=[f"approved change {change_id} ({change['subject']})" + (
            f"; still needs: {'; '.join(missing)}" if missing else f"; {change['author']} can now apply it")],
    )


def changes(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.changes``: changes waiting on approval or on their time, and what became of them."""
    try:
        ts = _targeting(_service())
    except _Refused as exc:
        return SkillResult(value={"changes": [], "targeting": str(exc)})
    statuses = _list(params.get("status")) or None
    return SkillResult(value={"changes": ts.changes(statuses=statuses), "targeting": ""})


def change_cancel(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.change_cancel``: cancel a change that waits on approval or on its time."""
    try:
        ts = _targeting(_service())
    except _Refused as exc:
        return _fail(exc)
    change_id = str(params.get("id") or "").strip()
    reason = str(params.get("reason") or "").strip()
    if not reason:
        return SkillResult(ok=False, errors=["give a reason: a cancelled change says why"])
    change = ts.change(change_id) if change_id else None
    if change is None or change["status"] not in ("awaiting_approval", "approved", "scheduled"):
        return SkillResult(ok=False, errors=[f"no waiting change {change_id!r}"])
    row = ts.update_change(change_id, status="cancelled", result={"cancelled_by": _actor(ctx), "reason": reason})
    note_change(kind="scheduled-change", subject=change["subject"], old=change["status"], new="cancelled")
    return SkillResult(value={"change": row}, actions_taken=[f"cancelled change {change_id} ({change['subject']})"])


# ------------------------------------------------------------------ the scheduler's tick

#: How a stored change is applied when its time comes.
APPLY: dict[str, Callable[[Mapping[str, Any], str], SkillResult]] = {
    "features.rollout_set": apply_rollout_set,
    "features.cohort_set": apply_cohort_set,
    "features.cohort_delete": apply_cohort_delete,
    "features.cohort_import": apply_cohort_import,
    "features.stage_plan_set": apply_stage_plan_set,
}


def _aware(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _notify_failed(change: Mapping[str, Any], error: str) -> None:
    try:
        import importlib

        from axiom.extensions.builtins.notifications.incidents.model import Cause, Unmet
        from axiom.extensions.builtins.notifications.skills.send import _ctx
        from axiom.governance import Classification

        from ..progressive import ACTOR, RECIPIENT

        notify = importlib.import_module("axiom.extensions.builtins.notifications.send")
        notify.send(_ctx(), actor=ACTOR, recipient=RECIPIENT,
                    payload=notify.NotificationPayload(
                        summary=f"A scheduled change to {change['subject']} did not apply.", body=error),
                    classification=Classification.INTERNAL, priority=notify.Priority.HIGH,
                    intent="features.change_apply",
                    cause=Cause(area="features", kind="scheduled_change_failed", action=change["capability"],
                                subject=change["subject"], error_class="ChangeRefused",
                                unmet=(Unmet(id="scheduled-change-applies", why=error,
                                             fix="plan the change again: preview it, then set it"),)))
    except Exception:  # noqa: BLE001 - the failure is recorded on the change whatever the alert path does
        pass


def _apply_due(ts: Any, ctx: SkillContext | None, now: datetime) -> tuple[list[str], list[str]]:
    from axiom.infra import audit_trail

    applied, failed = [], []
    for change in sorted(ts.changes(statuses=["scheduled"]), key=lambda c: c["run_at"] or ""):
        if not change["run_at"] or _aware(change["run_at"]) > now:
            continue
        fn = APPLY.get(change["capability"])
        audit_trail.take_change()
        try:
            if fn is None:
                raise _Refused(f"{change['capability']} cannot be applied on a schedule")
            result = fn(change["params"], change["author"])
            error = None if result.ok else "; ".join(result.errors) or "it was refused"
        except Exception as exc:  # noqa: BLE001 - a change that no longer applies fails and says why
            error = str(exc) or type(exc).__name__
        note = audit_trail.take_change()
        if error is None:
            ts.update_change(change["id"], status="applied", applied_at=now, result={"applied_by": change["author"]})
            applied.append(change["id"])
        else:
            ts.update_change(change["id"], status="failed", result={"error": error})
            failed.append(change["id"])
            _notify_failed(change, error)
        if ctx is not None and getattr(ctx, "state_dir", None) is not None:
            audit_trail.emit(ctx, skill="features.change_apply", params={"id": change["id"]},
                             outcome="ok" if error is None else "error", errors_count=0 if error is None else 1,
                             change=note)
    return applied, failed


def rollout_tick(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.rollout_tick``: one pass of the scheduler over scheduled changes, stage plans
    and following cohorts. ``now`` (ISO-8601) replays a pass at another time."""
    from .. import cohort_sources as cs
    from .. import progressive

    try:
        ts = _targeting(_service())
    except _Refused as exc:
        return SkillResult(value={"skipped": str(exc)})
    now = _aware(str(params["now"]).replace("Z", "+00:00")) if params.get("now") else datetime.now(UTC)
    applied, failed = _apply_due(ts, ctx, now)
    moved = progressive.advance_all(ts, now=now, ctx=ctx)
    refreshed, refresh_failed = [], []
    for key, cohort in sorted(ts.cohorts().items()):
        if cs.due(cohort, now):
            out = refresh_one(ts, key, now)
            (refresh_failed if out and out["error"] else refreshed).append(key)
    value = {"applied": applied, "failed": failed, **moved, "refreshed": refreshed,
             "refresh_failed": refresh_failed, "at": now.isoformat()}
    return SkillResult(value=value)


__all__ = [
    "APPLY",
    "approve",
    "change_cancel",
    "changes",
    "cohort_import",
    "cohort_import_preview",
    "cohort_refresh",
    "rollout_tick",
    "stage_plan_halt",
    "stage_plan_preview",
    "stage_plan_set",
]
