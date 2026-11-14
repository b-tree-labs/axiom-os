# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Flag storage: ``flag_overrides``, ``cohorts`` and ``rollouts`` behind one session seam.

Production binds ``axiom.infra.db.session_for("flags")`` (ADR-052) against the
runtime Postgres (ADR-174); tests bind SQLite through :func:`set_provider`.
The service, the skills and the HTTP route reach the tables through
:class:`OverrideStore` and :class:`TargetingStore`, and nothing else opens a
session on them. There is no cache here: every read is a query, so a kill
switch set on one node or surface applies on the next request everywhere
(LOOP-F6); the resolver memoises per request instead.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from .db_models import SCOPES, Change, Cohort, FlagOverride, Rollout, StagePlan


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("flags")


_provider: Callable[[], AbstractContextManager[Any]] = _default_provider


def set_provider(provider: Callable[[], AbstractContextManager[Any]]) -> None:
    """Bind the session provider (tests inject SQLite here)."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = _default_provider


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    """A session from the bound provider. Caller commits."""
    with _provider() as session:
        yield session


_COHORT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _check_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError(f"scope is one of {', '.join(SCOPES)}, not {scope!r}")


class OverrideStore:
    """Reads and writes ``flag_overrides`` rows."""

    def overrides_for(self, scope: str, scope_id: str | None) -> dict[str, str]:
        """``{key: value}`` stored for one node, account or user; empty when unnamed."""
        _check_scope(scope)
        if not scope_id:
            return {}
        with session_scope() as s:
            rows = s.query(FlagOverride).filter_by(scope=scope, scope_id=str(scope_id)).all()
            return {r.key: r.value for r in rows}

    def entries_for(self, scope: str, scope_id: str | None) -> dict[str, dict[str, Any]]:
        """``{key: row}`` with the reason and expiry (LOOP-F10); empty when unnamed."""
        _check_scope(scope)
        if not scope_id:
            return {}
        with session_scope() as s:
            rows = s.query(FlagOverride).filter_by(scope=scope, scope_id=str(scope_id)).all()
            return {r.key: _row(r) for r in rows}

    def put(
        self,
        scope: str,
        scope_id: str,
        key: str,
        value: str,
        *,
        by: str,
        reason: str | None = None,
        expires_at: datetime | None = None,
        variant: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one override; returns the stored row. ``variant`` is a multivariate flag's (LOOP-F17)."""
        _check_scope(scope)
        with session_scope() as s:
            row = s.get(FlagOverride, (scope, str(scope_id), key))
            now = datetime.now(UTC)
            if row is None:
                row = FlagOverride(scope=scope, scope_id=str(scope_id), key=key)
                s.add(row)
            row.value, row.updated_by, row.updated_at = value, by, now
            row.reason = (reason or "").strip() or None
            row.expires_at = expires_at
            row.variant = variant or None
            s.commit()
            return _row(row)

    def delete(self, scope: str, scope_id: str, key: str) -> bool:
        """Remove one override; ``True`` when there was one."""
        _check_scope(scope)
        with session_scope() as s:
            row = s.get(FlagOverride, (scope, str(scope_id), key))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def rows(self, key: str | None = None) -> list[dict[str, Any]]:
        """Every stored override (optionally for one key), scope-ordered."""
        with session_scope() as s:
            q = s.query(FlagOverride)
            if key:
                q = q.filter_by(key=key)
            rows = [_row(r) for r in q.all()]
        order = {sc: i for i, sc in enumerate(SCOPES)}
        return sorted(rows, key=lambda r: (r["key"], order[r["scope"]], r["scope_id"]))


    def subjects(self) -> dict[str, set[str]]:
        """The principals and accounts that have any stored override (a preview's population)."""
        with session_scope() as s:
            rows = s.query(FlagOverride.scope, FlagOverride.scope_id).distinct().all()
        out: dict[str, set[str]] = {"principal": set(), "account": set()}
        for scope, sid in rows:
            if scope == "user" and "/" not in sid:
                out["principal"].add(sid)
            elif scope == "account" and "/" not in sid:
                out["account"].add(sid)
        return out


def _iso(at: datetime | None) -> str | None:
    if at is None:
        return None
    if at.tzinfo is None:  # SQLite hands back naive datetimes; they were stored in UTC
        at = at.replace(tzinfo=UTC)
    return at.isoformat()


def _row(r: FlagOverride) -> dict[str, Any]:
    return {
        "scope": r.scope,
        "scope_id": r.scope_id,
        "key": r.key,
        "value": r.value,
        "updated_by": r.updated_by,
        "updated_at": _iso(r.updated_at),
        "reason": r.reason,
        "expires_at": _iso(r.expires_at),
        "variant": r.variant,
    }


# ------------------------------------------------------------------ cohorts and rollouts


def _cohort_row(c: Cohort) -> dict[str, Any]:
    principals, accounts = list(c.principals or []), list(c.accounts or [])
    return {
        "key": c.key,
        "name": c.name or "",
        "description": c.description or "",
        "rules": c.rules,
        "principals": principals,
        "accounts": accounts,
        #: How many it lists (LOOP-F16); a rule's members are counted only by a preview.
        "size": len(principals) + len(accounts),
        "source": c.source,
        "refresh_every_s": c.refresh_every_s,
        "refreshed_at": _iso(c.refreshed_at),
        "source_error": c.source_error,
        "reason": c.reason,
        "created_by": c.created_by,
        "created_at": _iso(c.created_at),
        "updated_by": c.updated_by,
        "updated_at": _iso(c.updated_at),
    }


def _rollout_row(r: Rollout) -> dict[str, Any]:
    return {
        "key": r.key,
        "environment": r.environment,
        "rules": r.rules,
        "bucket_by": r.bucket_unit or r.bucket_by,
        "bucket_unit": r.bucket_unit or r.bucket_by,
        "reason": r.reason,
        "kill_switch": bool(r.kill_switch),
        "kill_reason": r.kill_reason,
        "kill_by": r.kill_by,
        "kill_at": _iso(r.kill_at),
        "updated_by": r.updated_by,
        "updated_at": _iso(r.updated_at),
    }


def _ids(values: Any) -> list[str]:
    """Distinct, in first-seen order; a set beside the list keeps a large import linear (LOOP-F16)."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values or ():
        text = str(v).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


class TargetingStore:
    """Reads and writes the ``cohorts`` and ``rollouts`` tables (LOOP-F13: the record only).

    Writes validate what they store (:mod:`axiom.extensions.builtins.features.targeting`)
    and raise its ``RuleError``; the skills preview and plan before they call here (LOOP-F9).
    """

    available = True
    why = ""

    # -------------------------------------------------------------- reads

    def cohorts(self) -> dict[str, dict[str, Any]]:
        with session_scope() as s:
            return {c.key: _cohort_row(c) for c in s.query(Cohort).all()}

    def cohort(self, key: str) -> dict[str, Any] | None:
        with session_scope() as s:
            c = s.get(Cohort, key)
            return _cohort_row(c) if c is not None else None

    def rollouts(self, environment: str) -> dict[str, dict[str, Any]]:
        with session_scope() as s:
            return {r.key: _rollout_row(r) for r in s.query(Rollout).filter_by(environment=environment).all()}

    def rollout(self, key: str, environment: str) -> dict[str, Any] | None:
        with session_scope() as s:
            r = s.get(Rollout, (key, environment))
            return _rollout_row(r) if r is not None else None

    def all_rollouts(self) -> list[dict[str, Any]]:
        with session_scope() as s:
            return [_rollout_row(r) for r in s.query(Rollout).all()]

    def references(self, cohort: str) -> list[str]:
        """What names ``cohort``: other cohorts' rules and rollout rules, as readable labels."""
        from axiom.extensions.builtins.features.targeting import cohort_refs

        out = [f"cohort {k}" for k, c in self.cohorts().items() if k != cohort and cohort in cohort_refs(c["rules"])]
        for r in self.all_rollouts():
            for rule in r["rules"] or []:
                if rule.get("cohort") == cohort or cohort in cohort_refs(rule.get("match")):
                    out.append(f"{r['key']} ({r['environment']})")
                    break
        return out

    # -------------------------------------------------------------- validation

    def check_cohort(self, key: str, *, rules: Any, known_attributes: Any = None) -> None:
        from axiom.extensions.builtins.features import attributes
        from axiom.extensions.builtins.features import targeting as t

        if not _COHORT_KEY.match(key or ""):
            raise t.RuleError(f"cohort key {key!r} is a lowercase identifier (letters, digits, _ or -), up to 64 long")
        if rules is None:
            return
        stored = {k: c["rules"] for k, c in self.cohorts().items()}
        attrs = set(attributes.catalogue()) if known_attributes is None else set(known_attributes)
        t.validate_rule(rules, known_attributes=attrs, known_cohorts=set(stored) | {key})
        t.validate_cohort_graph(key, rules, stored)

    def check_rollout(self, rules: Any, *, bucket_by: Any = None, flag: Any = None) -> list[dict[str, Any]]:
        """The rules, validated against the catalogue, the stored cohorts and (when given) the
        flag's declared values; the unit is checked too (LOOP-F14)."""
        from axiom.extensions.builtins.features import attributes
        from axiom.extensions.builtins.features import targeting as t

        cohorts, attrs = set(self.cohorts()), set(attributes.catalogue())
        t.check_unit(bucket_by, known_attributes=attrs, known_cohorts=cohorts)
        return t.validate_rollout_rules(rules, known_cohorts=cohorts, known_attributes=attrs, flag=flag)

    # -------------------------------------------------------------- writes

    def put_cohort(
        self,
        key: str,
        *,
        by: str,
        name: str = "",
        description: str = "",
        rules: Any = None,
        principals: Any = (),
        accounts: Any = (),
        reason: str | None = None,
        validate: bool = True,
        source: Any = None,
        refresh_every_s: int | None = None,
        refreshed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create or replace one cohort; returns the stored row."""
        if validate:
            self.check_cohort(key, rules=rules)
        with session_scope() as s:
            c = s.get(Cohort, key)
            now = datetime.now(UTC)
            if c is None:
                c = Cohort(key=key, created_by=by, created_at=now)
                s.add(c)
            c.name, c.description, c.rules = name or "", description or "", rules
            c.principals, c.accounts = _ids(principals), _ids(accounts)
            c.reason = (reason or "").strip() or None
            c.source, c.refresh_every_s = (dict(source) if source else None), refresh_every_s
            c.refreshed_at = refreshed_at if source else None
            c.source_error = None
            c.updated_by, c.updated_at = by, now
            s.commit()
            return _cohort_row(c)

    def refreshed(self, key: str, *, principals: Any = None, error: str | None = None,
                  at: datetime | None = None) -> dict[str, Any] | None:
        """Record one refresh from a cohort's source (LOOP-F16): new members, or the error and the
        members kept as they were."""
        with session_scope() as s:
            c = s.get(Cohort, key)
            if c is None:
                return None
            c.refreshed_at = at or datetime.now(UTC)
            if error is None:
                c.principals, c.source_error = _ids(principals), None
            else:
                c.source_error = error
            s.commit()
            return _cohort_row(c)

    def delete_cohort(self, key: str) -> bool:
        with session_scope() as s:
            c = s.get(Cohort, key)
            if c is None:
                return False
            s.delete(c)
            s.commit()
            return True

    def _rollout_for_write(self, s: Any, key: str, environment: str, by: str) -> Rollout:
        r = s.get(Rollout, (key, environment))
        if r is None:
            r = Rollout(key=key, environment=environment, kill_switch=False, updated_by=by)
            s.add(r)
        return r

    def put_rollout(
        self,
        key: str,
        environment: str,
        *,
        rules: Any,
        by: str,
        reason: str | None = None,
        bucket_by: str | None = None,
        validate: bool = True,
    ) -> dict[str, Any]:
        """Replace one flag's rules in one environment (the kill switch is left as it is)."""
        if validate:
            rules = self.check_rollout(rules, bucket_by=bucket_by, flag=_declared_flag(key))
        unit = (str(bucket_by).strip().lower() or None) if bucket_by else None
        with session_scope() as s:
            r = self._rollout_for_write(s, key, environment, by)
            r.rules = list(rules) if rules is not None else None
            # The release before LOOP-F14 reads bucket_by, which holds only principal or account.
            r.bucket_by = unit if unit in ("principal", "account") else None
            r.bucket_unit = unit
            r.reason = (reason or "").strip() or None
            r.updated_by, r.updated_at = by, datetime.now(UTC)
            s.commit()
            return _rollout_row(r)

    def set_kill_switch(
        self, key: str, environment: str, engaged: bool, *, by: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Engage or release one flag's kill switch in one environment (the rules are left as they are)."""
        with session_scope() as s:
            r = self._rollout_for_write(s, key, environment, by)
            now = datetime.now(UTC)
            r.kill_switch = bool(engaged)
            r.kill_reason = (reason or "").strip() or None
            r.kill_by, r.kill_at = by, now
            r.updated_by, r.updated_at = by, now
            s.commit()
            return _rollout_row(r)


    # -------------------------------------------------------------- stage plans (LOOP-F18)

    def stage_plans(self, environment: str) -> dict[str, dict[str, Any]]:
        with session_scope() as s:
            return {p.key: _stage_row(p) for p in s.query(StagePlan).filter_by(environment=environment).all()}

    def all_stage_plans(self) -> list[dict[str, Any]]:
        with session_scope() as s:
            return [_stage_row(p) for p in s.query(StagePlan).all()]

    def stage_plan(self, key: str, environment: str) -> dict[str, Any] | None:
        with session_scope() as s:
            p = s.get(StagePlan, (key, environment))
            return _stage_row(p) if p is not None else None

    def put_stage_plan(self, key: str, environment: str, *, plan: dict[str, Any], by: str,
                       reason: str | None = None, stage: int = 0, now: datetime | None = None) -> dict[str, Any]:
        """Create or replace one plan; it starts (running) at ``stage`` now."""
        now = now or datetime.now(UTC)
        with session_scope() as s:
            p = s.get(StagePlan, (key, environment))
            history = list(p.history or []) if p is not None else []
            if p is None:
                p = StagePlan(key=key, environment=environment, updated_by=by)
                s.add(p)
            history.append({"at": now.isoformat(), "from": None, "to": stage, "status": "running", "by": by,
                            "why": (reason or "").strip() or None})
            p.plan, p.status, p.stage, p.stage_entered_at = dict(plan), "running", stage, now
            p.halted_reason, p.history = None, history
            p.reason = (reason or "").strip() or None
            p.updated_by, p.updated_at = by, now
            s.commit()
            return _stage_row(p)

    def move_stage_plan(self, key: str, environment: str, *, status: str, stage: int, by: str,
                        why: str | None = None, now: datetime | None = None) -> dict[str, Any] | None:
        """Record one transition: advance, halt, roll back or finish."""
        now = now or datetime.now(UTC)
        with session_scope() as s:
            p = s.get(StagePlan, (key, environment))
            if p is None:
                return None
            history = list(p.history or [])
            history.append({"at": now.isoformat(), "from": p.stage, "to": stage, "status": status, "by": by,
                            "why": why})
            if stage != p.stage:
                p.stage_entered_at = now
            p.status, p.stage, p.history = status, stage, history
            p.halted_reason = why if status in ("halted", "rolled_back") else None
            p.updated_by, p.updated_at = by, now
            s.commit()
            return _stage_row(p)

    def delete_stage_plan(self, key: str, environment: str) -> bool:
        with session_scope() as s:
            p = s.get(StagePlan, (key, environment))
            if p is None:
                return False
            s.delete(p)
            s.commit()
            return True

    # -------------------------------------------------------------- waiting changes (LOOP-F19, F23)

    def changes(self, *, statuses: Any = None) -> list[dict[str, Any]]:
        with session_scope() as s:
            q = s.query(Change)
            if statuses:
                q = q.filter(Change.status.in_(list(statuses)))
            rows = [_change_row(c) for c in q.all()]
        return sorted(rows, key=lambda r: r["created_at"] or "", reverse=True)

    def change(self, change_id: str) -> dict[str, Any] | None:
        with session_scope() as s:
            c = s.get(Change, change_id)
            return _change_row(c) if c is not None else None

    def open_change_for(self, plan_hash: str) -> dict[str, Any] | None:
        """The change still waiting on approval for this exact call, if any."""
        with session_scope() as s:
            c = (s.query(Change).filter_by(plan_hash=plan_hash)
                 .filter(Change.status.in_(["awaiting_approval", "approved"])).first())
            return _change_row(c) if c is not None else None

    def add_change(self, *, plan_hash: str, capability: str, params: dict[str, Any], subject: str,
                   environment: str | None, author: str, reason: str | None, policy: list[dict[str, Any]],
                   status: str, run_at: datetime | None = None, timezone: str | None = None) -> dict[str, Any]:
        import uuid

        now = datetime.now(UTC)
        with session_scope() as s:
            c = Change(id=uuid.uuid4().hex[:16], plan_hash=plan_hash, capability=capability, params=dict(params),
                       subject=subject, environment=environment, author=author, reason=reason, policy=list(policy),
                       approvals=[], run_at=run_at, timezone=timezone, status=status, created_at=now, updated_at=now)
            s.add(c)
            s.commit()
            return _change_row(c)

    def update_change(self, change_id: str, **fields: Any) -> dict[str, Any] | None:
        with session_scope() as s:
            c = s.get(Change, change_id)
            if c is None:
                return None
            for k, v in fields.items():
                setattr(c, k, v)
            c.updated_at = datetime.now(UTC)
            s.commit()
            return _change_row(c)


def _declared_flag(key: str | None) -> Any:
    if not key:
        return None
    from axiom.extensions.builtins.features.flag_registry import registry

    try:
        return registry().get(key)
    except Exception:  # noqa: BLE001 - no registry checks values as on/off/soon
        return None


def _stage_row(p: StagePlan) -> dict[str, Any]:
    return {
        "key": p.key,
        "environment": p.environment,
        "plan": p.plan,
        "status": p.status,
        "stage": p.stage,
        "stage_entered_at": _iso(p.stage_entered_at),
        "halted_reason": p.halted_reason,
        "history": list(p.history or []),
        "reason": p.reason,
        "updated_by": p.updated_by,
        "updated_at": _iso(p.updated_at),
    }


def _change_row(c: Change) -> dict[str, Any]:
    return {
        "id": c.id,
        "plan_hash": c.plan_hash,
        "capability": c.capability,
        "params": c.params,
        "subject": c.subject,
        "environment": c.environment,
        "author": c.author,
        "reason": c.reason,
        "policy": list(c.policy or []),
        "approvals": list(c.approvals or []),
        "run_at": _iso(c.run_at),
        "timezone": c.timezone,
        "status": c.status,
        "result": c.result,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
        "applied_at": _iso(c.applied_at),
    }


__all__ = ["OverrideStore", "TargetingStore", "reset_provider", "session_scope", "set_provider"]
