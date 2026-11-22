# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cohorts, rollouts, the kill switch and explain (ADR-257 D5, LOOP-F1 to F10).

Read verbs: ``flags.explain``, ``flags.cohorts``, ``flags.cohort_preview``,
``flags.rollout_preview``. Writes:

* ``flags.kill`` (reversible-write): engage or release one flag's kill switch in
  one environment. It applies at once and needs no plan, so an incident is stopped
  fast (LOOP-F6).
* ``flags.cohort_set``, ``flags.cohort_delete``, ``flags.rollout_set``
  (destructive): the platform's plan gate holds them on every surface but the
  command line until a person approves the plan (LOOP-F9). Preview first with the
  matching ``*_preview`` verb; the command line previews unless given ``--apply``.

Every write notes its before and after for the one audit record (``note_change``),
which the change log shows by kind (``cohort``, ``rollout``, ``kill-switch``).
The node file is never written here (LOOP-F13).

Every cohort, rollout and stage-plan write (LOOP-F18) also passes two more gates, in
:func:`write`: the approvals a policy asks for in that environment (LOOP-F23), and, when
it names ``at`` and ``timezone``, its time (LOOP-F19): it is then stored as a scheduled
change and the platform scheduler applies it (``flags.rollout_tick``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from axiom.infra.audit_trail import DEFAULT_PRINCIPAL, note_change
from axiom.infra.skills import SkillContext, SkillResult

from ._flags import _service, default_account, this_node

#: How many affected subjects a preview names.
SAMPLE = 10
#: A member list longer than this is audited by count, digest and sample, not in full (LOOP-F16).
AUDIT_LIST_LIMIT = 50


def _actor(ctx: SkillContext | None) -> str:
    return getattr(ctx, "actor", None) or DEFAULT_PRINCIPAL


def _env(params: Mapping[str, Any]) -> str:
    from ..flag_registry import get_environment

    return str(params.get("environment") or "").strip().lower() or get_environment()


def _json(value: Any, name: str) -> Any:
    """A parameter that may arrive as JSON text (the command line) or as data (MCP)."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} is not valid JSON: {exc.msg}") from exc
    return value


def _list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    return [str(v).strip() for v in value if str(v).strip()]


def who(params: Mapping[str, Any]) -> dict[str, Any]:
    """The subject a read resolves for: node, account, user, and what the caller knows of them."""
    roles = params.get("roles")
    return {
        "node": str(params.get("node") or "") or this_node(),
        "account": str(params.get("account") or "") or default_account(),
        "user": str(params.get("user") or "") or None,
        "roles": _list(roles) if roles is not None else None,
        "locale": str(params.get("locale") or "") or None,
        "principal_kind": str(params.get("principal_kind") or "") or None,
        "timezone": str(params.get("timezone") or "") or None,
        "country": str(params.get("country") or "") or None,
        "region": str(params.get("region") or "") or None,
    }


def _targeting(svc):
    ts = svc.targeting
    if not getattr(ts, "available", True) or not hasattr(ts, "put_rollout"):
        why = getattr(ts, "why", "") or "no cohort store is bound here"
        raise _Refused(f"cohorts and rollouts live in the database, and {why}")
    return ts


class _Refused(Exception):
    pass


def _declared(svc, key: str) -> None:
    if not key:
        raise _Refused("name the flag: key is required")
    if key not in svc.get_registry():
        raise _Refused(f"no flag {key!r} is declared; `flags list` names them")


def _fail(exc: Exception) -> SkillResult:
    return SkillResult(ok=False, errors=[str(exc)])


# ------------------------------------------------------------------ explain


def explain(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.explain``: why a flag has its value for one subject (LOOP-F8)."""
    from ..resolver import FlagError

    key = str(params.get("key") or "").strip()
    if not key:
        return SkillResult(ok=False, errors=["name the flag: key is required"])
    at = None
    if params.get("at"):
        try:
            at = datetime.fromisoformat(str(params["at"]).replace("Z", "+00:00"))
        except ValueError:
            return SkillResult(ok=False, errors=[f"at {params['at']!r} is not an ISO-8601 time"])
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
    try:
        return SkillResult(value=_service().explain(key, now=at, **who(params)))
    except FlagError as exc:
        return SkillResult(ok=False, errors=[f"{exc}; `flags list` names the declared flags"])


# ------------------------------------------------------------------ reads


def cohorts(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohorts``: the stored cohorts, and this environment's rollouts and kill switches."""
    svc = _service()
    env = _env(params)
    try:
        ts = _targeting(svc)
        stored = ts.cohorts()
        rollouts = ts.rollouts(env)
    except _Refused as exc:
        return SkillResult(value={"environment": env, "cohorts": [], "rollouts": [], "stage_plans": [],
                                  "targeting": str(exc)})
    rows = []
    for key, c in sorted(stored.items()):
        rows.append({**c, "used_by": ts.references(key)})
    plans = ts.stage_plans(env) if hasattr(ts, "stage_plans") else {}
    return SkillResult(value={"environment": env, "cohorts": rows,
                              "rollouts": [rollouts[k] for k in sorted(rollouts)],
                              "stage_plans": [plans[k] for k in sorted(plans)], "targeting": ""})


# ------------------------------------------------------------------ previews


class _Overlay:
    """The record as it would be after one write: one rollout or one cohort replaced."""

    available = True
    why = ""

    def __init__(self, base, *, rollout: tuple[str, str, dict | None] | None = None,
                 cohort: tuple[str, dict | None] | None = None,
                 stage: tuple[str, str, dict | None] | None = None) -> None:
        self.base, self._rollout, self._cohort, self._stage = base, rollout, cohort, stage

    def stage_plans(self, environment: str) -> dict[str, dict[str, Any]]:
        reader = getattr(self.base, "stage_plans", None)
        out = dict(reader(environment)) if reader is not None else {}
        if self._stage and self._stage[1] == environment:
            key, _, row = self._stage
            if row is None:
                out.pop(key, None)
            else:
                out[key] = row
        return out

    def rollouts(self, environment: str) -> dict[str, dict[str, Any]]:
        out = dict(self.base.rollouts(environment))
        if self._rollout and self._rollout[1] == environment:
            key, _, row = self._rollout
            if row is None:
                out.pop(key, None)
            else:
                out[key] = {**(out.get(key) or {"kill_switch": False}), **row}
        return out

    def cohorts(self) -> dict[str, dict[str, Any]]:
        out = dict(self.base.cohorts())
        if self._cohort:
            key, row = self._cohort
            if row is None:
                out.pop(key, None)
            else:
                out[key] = row
        return out


def _population(svc, extra: list[dict[str, Any]] | None = None,
                listed: Mapping[str, Any] | None = None) -> tuple[list[tuple[str | None, str | None]], str]:
    """The subjects a preview counts: principals and accounts this node knows of."""
    principals: set[str] = set()
    accounts: set[str] = set()
    try:
        known = svc.overrides.subjects() if hasattr(svc.overrides, "subjects") else {}
    except Exception:  # noqa: BLE001 - an unreadable store previews over the listed members only
        known = {}
    principals |= set(known.get("principal") or ())
    accounts |= set(known.get("account") or ())
    try:
        stored = svc.targeting.cohorts()
    except Exception:  # noqa: BLE001
        stored = {}
    for c in list(stored.values()) + ([dict(listed)] if listed else []):
        principals |= set(c.get("principals") or ())
        accounts |= set(c.get("accounts") or ())
    for s in extra or ():
        principals.add(str(s))
    subjects = [(p, None) for p in sorted(principals)] + [(None, a) for a in sorted(accounts)]
    source = ("the principals and accounts this node knows of: those with a stored override, those "
              "listed in a cohort, and any named in `subjects`")
    return subjects, source


def _label(subject: tuple[str | None, str | None]) -> str:
    principal, account = subject
    return principal if principal else f"account:{account}"


def _cohort_def(params: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "name": str(params.get("name") or ""),
        "description": str(params.get("description") or ""),
        "rules": _json(params.get("rules"), "rules"),
        "principals": _list(params.get("principals")),
        "accounts": _list(params.get("accounts")),
    }
    source = _json(params.get("source"), "source")
    if source:
        out["source"] = source
        if params.get("refresh_every") not in (None, ""):
            out["refresh_every"] = params["refresh_every"]
    return out


def _summary(values: list[str]) -> Any:
    """A member list as the audit and a preview show it: in full when short, else counted."""
    if len(values) <= AUDIT_LIST_LIMIT:
        return list(values)
    import hashlib

    digest = hashlib.sha256("\n".join(values).encode()).hexdigest()
    return {"count": len(values), "sha256": digest, "sample": list(values[:SAMPLE])}


def _cohort_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {k: row.get(k) for k in ("name", "description", "rules")}
    out["principals"] = _summary(list(row.get("principals") or []))
    out["accounts"] = _summary(list(row.get("accounts") or []))
    if row.get("source"):
        out["source"] = row["source"]
        out["refresh_every_s"] = row.get("refresh_every_s")
    if "size" in row:
        out["size"] = row["size"]
    return out


# ------------------------------------------------------------------ approvals and schedules (LOOP-F19, F23)


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def _flag_defs(svc, keys) -> list:
    reg = svc.get_registry()
    return [reg[k] for k in keys if k in reg]


def cohort_policies(svc, ts, key: str) -> list:
    """The approval policies a cohort change answers to: those of every flag and environment
    whose rollout or stage plan reads the cohort, directly or through another cohort."""
    from .. import approvals as ap
    from ..targeting import cohort_refs

    stored = ts.cohorts()
    readers = {key}
    grew = True
    while grew:
        grew = False
        for k, c in stored.items():
            if k not in readers and readers & cohort_refs(c.get("rules")):
                readers.add(k)
                grew = True
    by_env: dict[str, set[str]] = {}
    for r in ts.all_rollouts():
        for rule in r.get("rules") or []:
            if rule.get("cohort") in readers or readers & cohort_refs(rule.get("match")):
                by_env.setdefault(r["environment"], set()).add(r["key"])
    for p in ts.all_stage_plans() if hasattr(ts, "all_stage_plans") else []:
        if any(st.get("cohort") in readers for st in (p.get("plan") or {}).get("stages") or []):
            by_env.setdefault(p["environment"], set()).add(p["key"])
    out: list = []
    for env, keys in sorted(by_env.items()):
        for policy in ap.policies_for(_flag_defs(svc, sorted(keys)), env):
            if policy not in out:
                out.append(policy)
    return out


def _schedule(params: Mapping[str, Any]) -> tuple[datetime | None, str | None]:
    """``(run_at, timezone)`` for a change that names ``at``; ``(None, None)`` for an immediate one."""
    from .. import targeting as t

    if params.get("at") in (None, ""):
        return None, None
    tz = str(params.get("timezone") or "").strip() or None
    try:
        return t.local_instant(params["at"], tz), tz
    except t.RuleError as exc:
        raise _Refused(str(exc)) from exc


def _digest(capability: str, params: Mapping[str, Any]) -> str:
    from axiom.infra.skill_chain import plan_digest

    return plan_digest(capability, {k: v for k, v in params.items() if not str(k).startswith("_")})


def _stored_params(params: Mapping[str, Any]) -> dict[str, Any]:
    from axiom.infra.skill_declarations import APPROVAL_PARAM

    return {k: v for k, v in params.items() if k != APPROVAL_PARAM and not str(k).startswith("_")}


def write(capability: str, params: Mapping[str, Any], ctx: SkillContext | None, *, ts, policies: list,
          subject: str, environment: str | None, apply: Callable[[str], SkillResult]) -> SkillResult:
    """One cohort, rollout or stage-plan write through its approvals and its time.

    Held by a policy until enough other people approved this exact call (LOOP-F23); stored
    as a scheduled change when it names a time (LOOP-F19); otherwise applied now by
    ``apply(author)``. A change applied, scheduled or cancelled is spent: the same call
    again opens a new one.
    """
    from .. import approvals as ap

    author = _actor(ctx)
    run_at, tz = _schedule(params)
    reason = str(params.get("reason") or "").strip() or None
    digest = _digest(capability, params)
    change = None
    if policies:
        change = ts.open_change_for(digest)
        if change is None:
            change = ts.add_change(plan_hash=digest, capability=capability, params=_stored_params(params),
                                   subject=subject, environment=environment, author=author, reason=reason,
                                   policy=[p.as_dict() for p in policies], status="awaiting_approval",
                                   run_at=run_at, timezone=tz)
        missing = ap.outstanding(policies, change["approvals"], change["author"])
        if missing:
            fix = f"another person runs `{_cli()} flags approve {change['id']}`, then this same call again"
            return SkillResult(ok=False, errors=[f"{subject}: waiting on approval: {'; '.join(missing)}. Fix: {fix}"],
                               unmet=[{"id": "approvals", "why": "; ".join(missing), "fix": fix}],
                               value={"applied": False, "change": change})
    if run_at is not None:
        if change is not None:
            change = ts.update_change(change["id"], status="scheduled", run_at=run_at, timezone=tz)
        else:
            change = ts.add_change(plan_hash=digest, capability=capability, params=_stored_params(params),
                                   subject=subject, environment=environment, author=author, reason=reason,
                                   policy=[], status="scheduled", run_at=run_at, timezone=tz)
        note_change(kind="scheduled-change", subject=subject, old=None,
                    new={"id": change["id"], "capability": capability, "run_at": change["run_at"], "timezone": tz})
        return SkillResult(
            value={"applied": False, "scheduled": {"id": change["id"], "run_at": change["run_at"], "timezone": tz},
                   "subject": subject},
            actions_taken=[f"scheduled {subject} for {params.get('at')} {tz or ''}".rstrip() + f" ({change['id']})"],
        )
    result = apply(author)
    if change is not None and result.ok:
        ts.update_change(change["id"], status="applied", applied_at=datetime.now(UTC),
                         result={"applied_by": author})
        if isinstance(result.value, dict):
            result.value["approved_change"] = change["id"]
    return result


def _plan_cohort(params: Mapping[str, Any]) -> tuple[Any, Any, str, dict[str, Any], dict[str, Any] | None]:
    from .. import cohort_sources as cs
    from .. import targeting as t

    svc = _service()
    ts = _targeting(svc)
    key = str(params.get("key") or "").strip()
    after = _cohort_def(params)
    try:
        ts.check_cohort(key, rules=after["rules"])
        if after.get("source"):
            after["source"] = cs.check_source(after["source"])
            if "refresh_every" in after:
                after["refresh_every_s"] = t.parse_duration(after.pop("refresh_every"), "refresh_every")
    except (t.RuleError, cs.SourceError) as exc:
        raise _Refused(str(exc)) from exc
    return svc, ts, key, after, _cohort_view(ts.cohort(key))


def _fill_from_source(after: dict[str, Any]) -> None:
    """A cohort that follows a source takes its members from it when it is set (LOOP-F16)."""
    from .. import cohort_sources as cs

    if not after.get("source"):
        return
    try:
        after["principals"] = cs.fetch(after["source"])
    except cs.SourceError as exc:
        raise _Refused(str(exc)) from exc


def cohort_preview(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohort_preview``: the change, its members (count and sample), what uses it."""
    from ..attributes import Subject
    from ..resolver import CohortMatcher

    try:
        svc, ts, key, after, before = _plan_cohort(params)
        _fill_from_source(after)
        run_at, tz = _schedule(params)
    except (_Refused, ValueError) as exc:
        return _fail(exc)
    subjects, source = _population(svc, _list(params.get("subjects")), after)
    overlay = _Overlay(ts, cohort=(key, {"key": key, **after}))
    members: list[str] = []
    unknown = 0
    cohorts_after = overlay.cohorts()
    for principal, account in subjects:
        m = CohortMatcher(cohorts_after, Subject(principal=principal, account=account, node=this_node()))
        value = m.member(key)
        if value is True:
            members.append(_label((principal, account)))
        elif value is None:
            unknown += 1
    return SkillResult(value={
        "key": key,
        "applied": False,
        "before": before,
        "after": _cohort_view({**after, "size": len(after["principals"]) + len(after["accounts"])}),
        "scheduled_for": _when(run_at, tz),
        "members": {"count": len(members), "sample": members[:SAMPLE], "unknown": unknown},
        "population": {"size": len(subjects), "source": source},
        "used_by": ts.references(key),
    })


def cohort_set(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohort_set``: create or replace one cohort (destructive: plan, then apply)."""
    reason = str(params.get("reason") or "").strip()
    if not reason:
        return SkillResult(ok=False, errors=["give a reason: every cohort write records why"])
    try:
        svc, ts, key, after, before = _plan_cohort(params)
        return write("features.cohort_set", params, ctx, ts=ts, policies=cohort_policies(svc, ts, key),
                     subject=f"cohort {key}", environment=None,
                     apply=lambda by: apply_cohort_set(_stored_params(params), by))
    except (_Refused, ValueError) as exc:
        return _fail(exc)


def apply_cohort_set(params: Mapping[str, Any], by: str) -> SkillResult:
    """Write one cohort as planned (an approved or scheduled change applies through here too)."""
    _, ts, key, after, before = _plan_cohort(params)
    _fill_from_source(after)
    refreshed = datetime.now(UTC) if after.get("source") else None
    row = ts.put_cohort(key, by=by, reason=str(params.get("reason") or ""), validate=False,
                        refreshed_at=refreshed, **after)
    note_change(kind="cohort", subject=key, old=before, new=_cohort_view(row))
    return SkillResult(
        value={"key": key, "applied": True, "before": before, "after": _cohort_view(row), "updated_by": by},
        actions_taken=[f"{'replaced' if before else 'created'} cohort {key} ({row['size']} listed)"],
    )


def _when(run_at: datetime | None, tz: str | None) -> dict[str, Any] | None:
    if run_at is None:
        return None
    from zoneinfo import ZoneInfo

    local = run_at.astimezone(ZoneInfo(tz)) if tz else run_at
    return {"run_at": run_at.isoformat(), "timezone": tz, "local": local.isoformat(timespec="minutes")}


def cohort_delete(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.cohort_delete``: remove one cohort; refused while anything names it."""
    reason = str(params.get("reason") or "").strip()
    if not reason:
        return SkillResult(ok=False, errors=["give a reason: every cohort write records why"])
    key = str(params.get("key") or "").strip()
    try:
        svc = _service()
        ts = _targeting(svc)
    except _Refused as exc:
        return _fail(exc)
    if ts.cohort(key) is None:
        return SkillResult(ok=False, errors=[f"no cohort {key!r} is stored"])
    used = ts.references(key)
    if used:
        return SkillResult(ok=False, errors=[f"cohort {key!r} is in use by {', '.join(used)}; remove those first"])
    try:
        return write("features.cohort_delete", params, ctx, ts=ts, policies=cohort_policies(svc, ts, key),
                     subject=f"cohort {key}", environment=None,
                     apply=lambda by: apply_cohort_delete(_stored_params(params), by))
    except _Refused as exc:
        return _fail(exc)


def apply_cohort_delete(params: Mapping[str, Any], by: str) -> SkillResult:
    key = str(params.get("key") or "").strip()
    ts = _targeting(_service())
    before = _cohort_view(ts.cohort(key))
    if before is None:
        return SkillResult(ok=False, errors=[f"no cohort {key!r} is stored"])
    used = ts.references(key)
    if used:
        return SkillResult(ok=False, errors=[f"cohort {key!r} is in use by {', '.join(used)}; remove those first"])
    ts.delete_cohort(key)
    note_change(kind="cohort", subject=key, old=before, new=None)
    return SkillResult(value={"key": key, "applied": True, "before": before, "after": None},
                       actions_taken=[f"deleted cohort {key}"])


def _rollout_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None or row.get("rules") is None:
        return None
    return {"rules": row.get("rules"), "bucket_by": row.get("bucket_by")}


def _plan_rollout(params: Mapping[str, Any]):
    from .. import targeting as t

    svc = _service()
    key = str(params.get("key") or "").strip()
    _declared(svc, key)
    ts = _targeting(svc)
    env = _env(params)
    raw = _json(params.get("rules"), "rules")
    if raw is None:
        raise _Refused("give the rules: an ordered list, [] for none")
    bucket_by = str(params.get("bucket_by") or "").strip().lower() or None
    try:
        rules = ts.check_rollout(raw, bucket_by=bucket_by, flag=svc.get_registry()[key])
    except t.RuleError as exc:
        raise _Refused(str(exc)) from exc
    return svc, ts, key, env, rules, bucket_by, _rollout_view(ts.rollout(key, env))


def rollout_preview(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.rollout_preview``: before and after, who changes (count and sample), per rule."""
    from ..resolver import FlagService

    try:
        svc, ts, key, env, rules, bucket_by, before = _plan_rollout(params)
        run_at, tz = _schedule(params)
    except (_Refused, ValueError) as exc:
        return _fail(exc)
    after = {"rules": rules, "bucket_by": bucket_by}
    after_svc = FlagService(overrides=svc.overrides, targeting=_Overlay(ts, rollout=(key, env, after)))
    subjects, source = _population(svc, _list(params.get("subjects")))
    changed: list[str] = []
    per_rule = [0] * len(rules)
    for principal, account in subjects:
        w = {"node": this_node(), "user": principal, "account": account}
        old = svc.resolve_flag(key, **w)
        out = after_svc.explain(key, **w)
        if out["decided_by"] == "rollout" and "rule_index" in out["detail"]:
            per_rule[out["detail"]["rule_index"]] += 1
        if out["value"] != old:
            changed.append(_label((principal, account)))
    return SkillResult(value={
        "key": key,
        "environment": env,
        "applied": False,
        "before": before,
        "after": after,
        "scheduled_for": _when(run_at, tz),
        "changes": {"count": len(changed), "sample": changed[:SAMPLE]},
        "per_rule": [{"rule_index": i, "admitted": n} for i, n in enumerate(per_rule)],
        "population": {"size": len(subjects), "source": source},
    })


def rollout_set(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.rollout_set``: replace one flag's rules in one environment (destructive)."""
    from ..person_only import refusal

    refused = refusal(str(params.get("key") or "").strip(), "change the rollout", ctx)
    if refused:
        return SkillResult(ok=False, errors=[refused])
    reason = str(params.get("reason") or "").strip()
    if not reason:
        return SkillResult(ok=False, errors=["give a reason: every rollout write records why"])
    try:
        svc, ts, key, env, rules, bucket_by, before = _plan_rollout(params)
        from .. import approvals as ap

        return write("features.rollout_set", params, ctx, ts=ts,
                     policies=ap.policies_for(_flag_defs(svc, [key]), env), subject=f"{key} ({env})",
                     environment=env, apply=lambda by: apply_rollout_set(_stored_params(params), by))
    except (_Refused, ValueError) as exc:
        return _fail(exc)


def apply_rollout_set(params: Mapping[str, Any], by: str) -> SkillResult:
    """Write one rollout as planned (an approved or scheduled change applies through here too)."""
    svc, ts, key, env, rules, bucket_by, before = _plan_rollout(params)
    reason = str(params.get("reason") or "")
    row = ts.put_rollout(key, env, rules=rules, bucket_by=bucket_by, reason=reason, by=by, validate=False)
    after = _rollout_view(row)
    note_change(kind="rollout", subject=f"{key} ({env})", old=before, new=after)
    return SkillResult(
        value={"key": key, "environment": env, "applied": True, "before": before, "after": after,
               "kill_switch": row["kill_switch"], "updated_by": by},
        actions_taken=[f"set the {env} rollout of {key}: {len(rules)} rule(s)"],
    )


# ------------------------------------------------------------------ the kill switch


def _truthy(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "release")


def kill(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.kill``: engage (default) or release one flag's kill switch in one environment.

    Reversible and immediate: no plan, so an incident is stopped on the next request
    (LOOP-F6). Engaging it needs a reason.
    """
    from ..person_only import refusal

    refused = refusal(str(params.get("key") or "").strip(), "pull or release the kill switch", ctx)
    if refused:
        return SkillResult(ok=False, errors=[refused])
    svc = _service()
    key = str(params.get("key") or "").strip()
    try:
        _declared(svc, key)
        ts = _targeting(svc)
    except _Refused as exc:
        return _fail(exc)
    env = _env(params)
    engaged = _truthy(params.get("engaged"), True)
    reason = str(params.get("reason") or "").strip()
    if engaged and not reason:
        return SkillResult(ok=False, errors=["give a reason: a kill switch says why it was pulled"])
    before = ts.rollout(key, env)
    was = "on" if before and before["kill_switch"] else "off"
    by = _actor(ctx)
    row = ts.set_kill_switch(key, env, engaged, reason=reason or None, by=by)
    now = "on" if row["kill_switch"] else "off"
    note_change(kind="kill-switch", subject=f"{key} ({env})", old=was, new=now)
    return SkillResult(
        value={"key": key, "environment": env, "kill_switch": row["kill_switch"], "reason": row["kill_reason"],
               "set_by": by, "value": svc.resolve_flag(key, node=this_node())},
        actions_taken=[
            f"engaged the kill switch of {key} in {env}: it resolves off for everyone"
            if engaged else f"released the kill switch of {key} in {env}"
        ],
    )


__all__ = [
    "apply_cohort_delete",
    "apply_cohort_set",
    "apply_rollout_set",
    "cohort_policies",
    "write",
    "cohort_delete",
    "cohort_preview",
    "cohort_set",
    "cohorts",
    "explain",
    "kill",
    "rollout_preview",
    "rollout_set",
    "who",
]
