# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Server-authoritative flag resolution (ADR-210, ADR-254, ADR-257 D5).

One order, most decisive first (LOOP-F7); the first step that decides gives the
value, and ``sources[key]`` names that step:

1. ``extension-off``: the owning extension is switched off on this node (ADR-254)
2. ``kill-switch``: the flag's kill switch in this environment (LOOP-F6)
3. ``prerequisite``: a flag this one requires does not resolve to what it needs for this
   subject (LOOP-F20); the flag serves its off value
4. ``user``: the user override
5. ``account``: the account override
6. ``rollout``: the flag's rollout rules in this environment, first rule that decides (LOOP-F4),
   then the current stage of its stage plan (LOOP-F18)
7. ``node``: the node override
8. ``variant``: the variant's flag set (``[variant.<name>.flags]``, MVP-12-A1)
9. ``env``: ``AXIOM_FLAG_<KEY>``
10. ``default``: the registry default for this environment (``default_by_env``, else ``default``)

A multivariate flag (LOOP-F17) resolves to a variant name in ``flags`` and to the
variant's value in ``values``; its "off" (kill switch, failed prerequisite, off
extension) is its off variant. A percentage or a split buckets by the rule's unit
(LOOP-F14): any catalogue attribute or a list of cohorts; an UNKNOWN unit is never admitted.

A flag of an off extension may still be set (owner decision 2026-11-12): the
value is stored and **waits on the extension**. The resolution says so
(``waiting_on``: key -> extension) and carries what steps 2 to 9 give it
(``staged``: key -> (value, source)), which applies the moment the extension is on.

An override carries an optional reason and expiry (LOOP-F10). An expired one is
ignored; in an environment where the flag is ``protected_in``, only a reasoned
one applies. Either way :meth:`FlagService.explain` reports it as ignored, with why.
A flag nobody declared resolves to ``off``.

Cohorts, rollouts and kill switches live only in the database (LOOP-F13). Every
resolution reads them afresh (no process cache, so a kill switch applies on the next
request) and memoises them for the one request. A node with no database, or a
record that cannot be read, resolves without them and says so (``targeting``).

Each isolated variant keeps its own stored overrides (MVP-12-A4): an override
set in one is written under that variant's namespace and never read by
another. The variant that takes an install over in place reads and writes the
install's own overrides, so nothing set before it is lost.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from axiom.extensions.builtins.flags.db_models import SCOPES

from . import targeting as t
from .attributes import Subject, lookup_for
from .flag_registry import ENV_PREFIX, OFF, ON, FlagDef, get_environment, normalize_value
from .flag_registry import registry as _registry

#: The source a resolution reports when the owning extension is off.
EXTENSION_OFF = "extension-off"
#: The source when the flag's kill switch is engaged in this environment (LOOP-F6).
KILL_SWITCH = "kill-switch"
#: The source when a rollout rule decided (LOOP-F4).
ROLLOUT = "rollout"
#: The source when a flag this one requires was not met for the subject (LOOP-F20).
PREREQUISITE = "prerequisite"

#: Every source a resolution can report, most decisive first (LOOP-F7).
ORDER: tuple[str, ...] = (
    EXTENSION_OFF, KILL_SWITCH, PREREQUISITE, "user", "account", ROLLOUT, "node", "variant", "env", "default",
)

NO_DATABASE = (
    "this node has no database, so flags resolve without cohorts, rollouts or kill switches "
    "(from the registry, the environment and the node's variant)"
)


class OverrideReader(Protocol):
    def overrides_for(self, scope: str, scope_id: str | None) -> dict[str, str]: ...
    def put(self, scope: str, scope_id: str, key: str, value: str, *, by: str, **kw: Any) -> dict[str, Any]: ...
    def delete(self, scope: str, scope_id: str, key: str) -> bool: ...


class TargetingReader(Protocol):
    """Where cohorts and rollouts are read (``flags.store.TargetingStore``)."""

    def rollouts(self, environment: str) -> dict[str, dict[str, Any]]: ...
    def cohorts(self) -> dict[str, dict[str, Any]]: ...


class NoTargeting:
    """The targeting reader of a node that has no database: nothing to read, and why."""

    available = False

    def __init__(self, why: str = NO_DATABASE) -> None:
        self.why = why

    def rollouts(self, environment: str) -> dict[str, dict[str, Any]]:
        return {}

    def cohorts(self) -> dict[str, dict[str, Any]]:
        return {}

    def stage_plans(self, environment: str) -> dict[str, dict[str, Any]]:
        return {}


class FlagError(ValueError):
    """A set the service refuses. ``code`` is what an HTTP face maps to a status."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code  # "unknown" | "protected" | "invalid"


class NoOverrides:
    """The override reader of a node that has no database (ADR-164: a role with no
    database function). Nothing is stored, so every layer is empty and a write is
    refused with the reason, instead of a connection attempt that fails."""

    def overrides_for(self, scope: str, scope_id: str | None) -> dict[str, str]:
        return {}

    def entries_for(self, scope: str, scope_id: str | None) -> dict[str, dict[str, Any]]:
        return {}

    def rows(self, key: str | None = None) -> list[dict[str, Any]]:
        return []

    def subjects(self) -> dict[str, set[str]]:
        return {"principal": set(), "account": set()}

    def put(self, scope: str, scope_id: str, key: str, value: str, *, by: str, **kw: Any) -> dict[str, Any]:
        raise FlagError(
            "this node's role has no database, so a flag override cannot be stored here; "
            "set the flag with an environment variable or on a node that holds the database",
            code="invalid",
        )

    def delete(self, scope: str, scope_id: str, key: str) -> bool:
        return False


@dataclass(frozen=True)
class Resolution:
    environment: str
    flags: dict[str, str]
    sources: dict[str, str]
    #: key -> the off extension a flag waits on (its source is ``extension-off``).
    waiting_on: dict[str, str] = field(default_factory=dict)
    #: key -> (value, source) the layers give it: what applies once that extension is on.
    staged: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: ``""`` when cohorts, rollouts and kill switches were read; else why they were not (LOOP-F13).
    targeting: str = ""
    #: key -> the value of the variant a multivariate flag resolved to (LOOP-F17).
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """What decided one flag for one subject, and everything explain reports about it."""

    value: str
    source: str
    detail: dict[str, Any] = field(default_factory=dict)
    ignored: list[dict[str, Any]] = field(default_factory=list)
    evaluated: list[dict[str, Any]] = field(default_factory=list)
    #: Each prerequisite, whether it was met, and its own chain (LOOP-F20).
    prerequisites: list[dict[str, Any]] = field(default_factory=list)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


class CohortMatcher:
    """Cohort membership for ONE subject, memoised for the life of one request.

    Each cohort is computed at most once however many rules or flags name it, and the
    attributes are read lazily (a cohort that lists its members never reads one). A cycle
    can only come from a hand-edited table (saves refuse them); it reads UNKNOWN.
    """

    def __init__(self, cohorts: Mapping[str, Mapping[str, Any]], subject: Subject) -> None:
        self.cohorts = cohorts
        self.subject = subject
        self.memo: dict[str, bool | None] = {}
        self.deps: dict[str, list[str]] = {}
        self.unknown: list[str] = []
        self._lookup = None
        self._stack: list[str] = []
        self._trace: list[str] = []

    def _look(self, name: str) -> t.Attr:
        if self._lookup is None:
            self._lookup = lookup_for(self.subject)
        return self._lookup(name)

    def _note(self, names: Iterable[str]) -> None:
        for n in names:
            if n not in self.unknown:
                self.unknown.append(n)

    def _listed(self, cohort: Mapping[str, Any], field_name: str) -> frozenset[str]:
        # A cohort may list tens of thousands (LOOP-F16): look it up as a set, built once a request.
        cache = self.__dict__.setdefault("_sets", {})
        k = (id(cohort), field_name)
        if k not in cache:
            cache[k] = frozenset(cohort.get(field_name) or ())
        return cache[k]

    def _compute(self, cohort: Mapping[str, Any]) -> bool | None:
        s = self.subject
        if s.principal and s.principal in self._listed(cohort, "principals"):
            return True
        if s.account and s.account in self._listed(cohort, "accounts"):
            return True
        if cohort.get("rules"):
            res = t.evaluate(cohort["rules"], self._look, self.member)
            self._note(res.unknown)
            return res.value
        return False

    def member(self, key: str) -> bool | None:
        self._trace.append(key)
        if key in self.memo:
            return self.memo[key]
        if key in self._stack:
            return None
        cohort = self.cohorts.get(key)
        if cohort is None:
            self.memo[key], self.deps[key] = None, []
            return None
        self._stack.append(key)
        mark = len(self._trace)
        try:
            value = self._compute(cohort)
        finally:
            self._stack.pop()
        self.deps[key] = list(dict.fromkeys(self._trace[mark:]))
        self.memo[key] = value
        return value

    def _expand(self, keys: Iterable[str]) -> dict[str, bool | None]:
        out: dict[str, bool | None] = {}
        todo = list(keys)
        while todo:
            k = todo.pop(0)
            if k in out:
                continue
            out[k] = self.memo.get(k)
            todo.extend(self.deps.get(k, []))
        return out

    def evaluate(self, expr: Mapping[str, Any]) -> tuple[bool | None, dict[str, bool | None]]:
        """``(value, cohorts_matched)``: every cohort consulted, nested ones included."""
        mark = len(self._trace)
        res = t.evaluate(expr, self._look, self.member)
        self._note(res.unknown)
        return res.value, self._expand(dict.fromkeys(self._trace[mark:]))

    def member_of(self, cohort: Mapping[str, Any]) -> bool | None:
        """Membership of an unsaved definition (a preview)."""
        value = self._compute(cohort)
        return value


class _Request:
    """One resolution: the subject, and everything read for it, read at most once."""

    def __init__(self, svc: FlagService, subject: Subject, *, now: datetime | None = None) -> None:
        from dataclasses import replace

        self.svc = svc
        self.env = get_environment()
        self.now = now or datetime.now(UTC)
        self.subject = replace(subject, at=self.now)
        self._stage_plans: dict[str, dict[str, Any]] | None = None
        self.decisions: dict[str, Decision] = {}
        self.deciding: set[str] = set()
        self.variant_flags = svc._variant_flags()
        self.off = svc.off_owners()
        self._entries: dict[str, dict[str, dict[str, Any]]] = {}
        self._rollouts: dict[str, dict[str, Any]] | None = None
        self._cohorts: dict[str, dict[str, Any]] | None = None
        self._matcher: CohortMatcher | None = None
        self.targeting = "" if getattr(svc.targeting, "available", True) else str(
            getattr(svc.targeting, "why", "") or NO_DATABASE
        )

    def entries(self, scope: str) -> dict[str, dict[str, Any]]:
        if scope not in self._entries:
            sid = {"node": self.subject.node, "account": self.subject.account,
                   "user": self.subject.principal}[scope]
            sid = self.svc._sid(sid)
            reader = self.svc.overrides
            if hasattr(reader, "entries_for"):
                self._entries[scope] = reader.entries_for(scope, sid)
            else:  # an older reader: values only
                self._entries[scope] = {k: {"value": v} for k, v in reader.overrides_for(scope, sid).items()}
        return self._entries[scope]

    def rollouts(self) -> dict[str, dict[str, Any]]:
        if self._rollouts is None:
            if self.targeting:
                self._rollouts = {}
            else:
                try:
                    self._rollouts = self.svc.targeting.rollouts(self.env)
                except Exception as exc:  # noqa: BLE001 - an unreadable record resolves without it, and says so
                    self._rollouts = {}
                    self.targeting = (
                        f"cohorts, rollouts and kill switches could not be read ({_first_line(exc)}), "
                        "so flags resolved without them"
                    )
        return self._rollouts

    def cohorts(self) -> dict[str, dict[str, Any]]:
        if self._cohorts is None:
            if self.targeting:
                self._cohorts = {}
            else:
                try:
                    self._cohorts = self.svc.targeting.cohorts()
                except Exception as exc:  # noqa: BLE001
                    self._cohorts = {}
                    self.targeting = (
                        f"cohorts could not be read ({_first_line(exc)}), so rules naming them did not match"
                    )
        return self._cohorts

    def stage_plans(self) -> dict[str, dict[str, Any]]:
        if self._stage_plans is None:
            reader = getattr(self.svc.targeting, "stage_plans", None)
            if self.targeting or reader is None:
                self._stage_plans = {}
            else:
                try:
                    self._stage_plans = reader(self.env)
                except Exception as exc:  # noqa: BLE001 - resolve without stage plans, and say so
                    self._stage_plans = {}
                    self.targeting = f"stage plans could not be read ({_first_line(exc)})"
        return self._stage_plans

    def matcher(self) -> CohortMatcher:
        if self._matcher is None:
            self._matcher = CohortMatcher(self.cohorts(), self.subject)
        return self._matcher

    def unit(self, flag: FlagDef, rule: Mapping[str, Any] | None = None) -> str:
        """The unit a percentage or split buckets (LOOP-F14): the rule's, the rollout row's,
        else the flag's declared one."""
        ro = self.rollouts().get(flag.key) or {}
        return str((rule or {}).get("bucket_by") or ro.get("bucket_unit") or ro.get("bucket_by") or flag.bucket_by)

    def unit_value(self, unit: str) -> Any:
        """The subject's value for ``unit``; ``None`` when it is UNKNOWN."""
        if unit == "principal":
            return self.subject.principal
        if unit == "account":
            return self.subject.account
        cohorts = t.unit_cohorts(unit)
        if cohorts:
            return next((c for c in cohorts if self.matcher().member(c) is True), None)
        attr = self.matcher()._look(unit)
        return attr.value if attr.available else None

    def bucket_subject(self, flag: FlagDef, rule: Mapping[str, Any] | None = None) -> tuple[str, str | None]:
        """``(unit, bucket id)``; the id is ``None`` when the unit is UNKNOWN."""
        unit = self.unit(flag, rule)
        return unit, t.unit_key(unit, self.unit_value(unit))


def _stage_view(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Where a stage plan stands, as explain and a decision report it (LOOP-F18)."""
    if not plan:
        return None
    stages = (plan.get("plan") or {}).get("stages") or []
    return {"index": plan.get("stage"), "of": len(stages), "status": plan.get("status")}


def waiting_text(extension: str) -> str:
    """The words every surface uses for a flag held by an off extension."""
    return f"waiting on extension {extension}"


def env_override(key: str, environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return normalize_value(env.get(f"{ENV_PREFIX}{key.upper()}"))


_pinned_off: frozenset[str] | None = None


def switched_off_owners() -> frozenset[str]:
    """The optional extensions that are off on this node (pinned in tests)."""
    if _pinned_off is not None:
        return _pinned_off
    try:
        from axiom.infra.optional_extensions import switched_off

        return switched_off()
    except Exception:  # noqa: BLE001 - no discovery means nothing is known to be off
        return frozenset()


def set_switched_off_owners(names: Iterable[str] | None) -> None:
    """Pin the off set (tests), or ``None`` to read the node file again."""
    global _pinned_off
    _pinned_off = None if names is None else frozenset(names)


class FlagService:
    """Resolve flags from the registry, the environment, stored overrides and the switchboard."""

    ENV_PREFIX = ENV_PREFIX

    def __init__(
        self,
        overrides: OverrideReader | None = None,
        *,
        registry: Mapping[str, FlagDef] | None = None,
        variant: Any = "active",
        targeting: TargetingReader | None = None,
    ) -> None:
        from axiom.extensions.builtins.flags.store import OverrideStore, TargetingStore

        if overrides is None:
            overrides = OverrideStore()
        self.overrides = overrides
        if targeting is None:
            if isinstance(overrides, NoOverrides):
                targeting = NoTargeting()
            elif isinstance(overrides, OverrideStore):
                targeting = TargetingStore()
            else:  # a reader bound by hand (tests): no record to read cohorts from
                targeting = NoTargeting("no cohort store is bound to this flag service")
        self.targeting = targeting
        self._registry = registry
        #: ``"active"`` reads ``AXIOM_VARIANT`` per call; ``None`` is a plain
        #: install; a :class:`axiom.infra.variants.Variant` pins one.
        self._variant = variant

    def variant(self):
        if self._variant == "active":
            from axiom.infra.variants import active_variant

            return active_variant()
        return self._variant

    def _sid(self, scope_id: str | None) -> str | None:
        """The stored scope id: namespaced by an isolated variant."""
        v = self.variant()
        ns = v.override_namespace if v is not None else None
        if not scope_id or not ns:
            return scope_id
        return f"variant:{ns}/{scope_id}"

    def _variant_flags(self) -> Mapping[str, str]:
        v = self.variant()
        return dict(v.flags) if v is not None else {}

    def get_registry(self) -> Mapping[str, FlagDef]:
        return self._registry if self._registry is not None else _registry()

    def off_owners(self) -> frozenset[str]:
        """The extensions whose flags resolve off here (ADR-254)."""
        return switched_off_owners()

    # ------------------------------------------------------------ resolve

    @staticmethod
    def subject(
        *,
        node: str | None = None,
        account: str | None = None,
        user: str | None = None,
        roles: Iterable[str] | None = None,
        locale: str | None = None,
        principal_kind: str | None = None,
        timezone: str | None = None,
        country: str | None = None,
        region: str | None = None,
    ) -> Subject:
        return Subject(
            principal=user or None,
            account=account or None,
            node=node or None,
            roles=tuple(str(r) for r in roles) if roles is not None else None,
            locale=locale or None,
            principal_kind=principal_kind or None,
            timezone=timezone or None,
            country=country or None,
            region=region or None,
        )

    def resolve(self, **who: Any) -> Resolution:
        """Every declared flag's value for this caller, and what decided each value."""
        req = _Request(self, self.subject(**who))
        flags: dict[str, str] = {}
        sources: dict[str, str] = {}
        waiting: dict[str, str] = {}
        staged: dict[str, tuple[str, str]] = {}
        values: dict[str, Any] = {}
        for key, flag in sorted(self.get_registry().items()):
            if flag.owner and flag.owner in req.off:
                flags[key], sources[key] = flag.off_value, EXTENSION_OFF
                waiting[key] = flag.owner
                d = self._decide(flag, req)
                staged[key] = (d.value, d.source)
            else:
                d = self._decide(flag, req)
                flags[key], sources[key] = d.value, d.source
            if flag.multivariate:
                values[key] = flag.variant_value(flags[key])
        return Resolution(environment=req.env, flags=flags, sources=sources, waiting_on=waiting,
                          staged=staged, targeting=req.targeting, values=values)

    def resolve_flag(self, key: str, **who: Any) -> str:
        """One flag's value without resolving the rest; ``off`` when undeclared."""
        flag = self.get_registry().get(key)
        if flag is None:
            return OFF
        req = _Request(self, self.subject(**who))
        if flag.owner and flag.owner in req.off:
            return flag.off_value
        return self._decide(flag, req).value

    def is_enabled(self, key: str, **who: Any) -> bool:
        return self.resolve_flag(key, **who) == ON

    def explain(self, key: str, *, now: datetime | None = None, **who: Any) -> dict[str, Any]:
        """Why ``key`` has its value for this subject (LOOP-F8).

        Raises :class:`FlagError` (``unknown``) for an undeclared flag.
        """
        flag = self.get_registry().get(key)
        if flag is None:
            raise FlagError(f"no flag {key!r} is declared", code="unknown")
        req = _Request(self, self.subject(**who), now=now)
        d = self._decide(flag, req)
        waiting = flag.owner if flag.owner and flag.owner in req.off else None
        ro = req.rollouts().get(key) or {}
        bucket_by, bucket_subject = req.bucket_subject(flag)
        value = flag.off_value if waiting else d.value
        plan = req.stage_plans().get(key)
        out: dict[str, Any] = {
            "flag": key,
            "kind": flag.kind or None,
            "environment": req.env,
            "subject": req.subject.as_dict(),
            "value": value,
            "variant": value if flag.multivariate else None,
            "variant_value": flag.variant_value(value) if flag.multivariate else None,
            "prerequisites": d.prerequisites,
            "stage": _stage_view(plan),
            "decided_by": EXTENSION_OFF if waiting else d.source,
            "detail": {"extension": waiting} if waiting else d.detail,
            "waiting_on": waiting,
            "staged": {"value": d.value, "source": d.source} if waiting else None,
            "order": list(ORDER),
            "protected": flag.is_protected(req.env),
            "kill_switch": bool(ro.get("kill_switch")),
            "rules": list(ro.get("rules") or []),
            "bucket_by": bucket_by,
            "unit_value": req.unit_value(bucket_by),
            "bucket": t.bucket(key, bucket_subject) if bucket_subject else None,
            "evaluated": d.evaluated,
            "ignored": d.ignored,
            "unknown_attributes": list(req._matcher.unknown) if req._matcher else [],
            "cohorts": {"available": not req.targeting, "why": req.targeting},
        }
        return out

    def _override(self, req: _Request, scope: str, flag: FlagDef, protected: bool,
                  ignored: list[dict[str, Any]]) -> t.OverrideView | None:
        view = t.read_override(req.entries(scope).get(flag.key), protected=protected, now=req.now, flag=flag)
        if view is None:
            return None
        if view.applies:
            return view
        ignored.append(view.ignored(scope))
        return None

    def _decide(self, flag: FlagDef, req: _Request) -> Decision:
        """Steps 2 to 10 of the order (the extension-off rule is the caller's), once a request."""
        if flag.key in req.decisions:
            return req.decisions[flag.key]
        if flag.key in req.deciding:  # a prerequisite cycle (refused on load; a pinned registry may hold one)
            return Decision(flag.off_value, PREREQUISITE, {"cycle": True})
        req.deciding.add(flag.key)
        try:
            d = self._decide_once(flag, req)
        finally:
            req.deciding.discard(flag.key)
        req.decisions[flag.key] = d
        return d

    def _prerequisite(self, need: Any, req: _Request) -> dict[str, Any]:
        """One prerequisite for this subject: met or not, and the other flag's own chain."""
        other = self.get_registry().get(need.flag)
        info: dict[str, Any] = {"flag": need.flag, "required": list(need.values), "declared": other is not None}
        if other is None:
            return {**info, "met": False, "value": None, "decided_by": None, "prerequisites": []}
        if other.owner and other.owner in req.off:
            value, source, chain = other.off_value, EXTENSION_OFF, []
        else:
            d = self._decide(other, req)
            value, source, chain = d.value, d.source, d.prerequisites
        return {**info, "met": value in need.values, "value": value, "decided_by": source, "prerequisites": chain}

    def _decide_once(self, flag: FlagDef, req: _Request) -> Decision:
        protected = flag.is_protected(req.env)
        ignored: list[dict[str, Any]] = []
        ro = req.rollouts().get(flag.key) or {}
        chain: list[dict[str, Any]] = []

        def done(value: str, source: str, evaluated: list | None = None, **detail: Any) -> Decision:
            return Decision(value, source, {k: v for k, v in detail.items() if v is not None},
                            ignored, evaluated or [], chain)

        if ro.get("kill_switch"):
            return done(flag.off_value, KILL_SWITCH, reason=ro.get("kill_reason"), set_by=ro.get("kill_by"),
                        at=ro.get("kill_at"))
        chain.extend(self._prerequisite(need, req) for need in flag.requires)
        unmet = [c["flag"] for c in chain if not c["met"]]
        if unmet:
            return done(flag.off_value, PREREQUISITE, unmet=unmet)
        for scope in ("user", "account"):
            view = self._override(req, scope, flag, protected, ignored)
            if view is not None:
                return done(view.value or flag.off_value, scope, reason=view.reason, expires_at=view.expires_at,
                            set_by=view.set_by)
        evaluated: list[dict[str, Any]] = []
        decided = self._rollout(flag, req, ro, ignored, evaluated)
        if decided is not None:
            value, detail = decided
            return done(value, ROLLOUT, evaluated, **detail)
        view = self._override(req, "node", flag, protected, ignored)
        if view is not None:
            return done(view.value or flag.off_value, "node", evaluated, reason=view.reason,
                        expires_at=view.expires_at, set_by=view.set_by)
        from_variant = flag.normalize((req.variant_flags or {}).get(flag.key))
        if from_variant is not None:
            return done(from_variant, "variant", evaluated)
        variable = f"{ENV_PREFIX}{flag.key.upper()}"
        from_env = flag.normalize(os.environ.get(variable)) if os.environ.get(variable) is not None else None
        if from_env is not None:
            return done(from_env, "env", evaluated, variable=variable)
        return done(flag.default_for(req.env), "default", evaluated)

    def _rollout(self, flag: FlagDef, req: _Request, ro: Mapping[str, Any],
                 ignored: list[dict[str, Any]], evaluated: list[dict[str, Any]]) -> tuple[str, dict] | None:
        """The first rollout rule that decides, as ``(value, detail)``; then the current stage of
        the flag's stage plan (LOOP-F18); ``None`` when neither does."""
        decided = self._rules(flag, req, ro.get("rules") or [], ignored, evaluated)
        if decided is not None:
            return decided
        plan = req.stage_plans().get(flag.key)
        rules = t.stage_rules(plan) if plan else []
        if not rules:
            return None
        decided = self._rules(flag, req, rules, ignored, evaluated, origin="stage")
        if decided is None:
            return None
        value, detail = decided
        return value, {**detail, "stage": _stage_view(plan)}

    def _rules(self, flag: FlagDef, req: _Request, rules: list[Mapping[str, Any]], ignored: list[dict[str, Any]],
               evaluated: list[dict[str, Any]], origin: str = "rule") -> tuple[str, dict] | None:
        for i, rule in enumerate(rules):
            label = {"rule_index": i} if origin == "rule" else {"stage_rule": i}
            try:
                if not t.rule_active(rule, req.now):
                    evaluated.append({**label, "status": "outside_window"})
                    continue
            except t.RuleError:
                ignored.append({"step": ROLLOUT, "status": "invalid_window", **label})
                continue
            cohort, expr = rule.get("cohort"), rule.get("match")
            matched: dict[str, bool | None] = {}
            if cohort is None and expr is None:
                member: bool | None = True
            else:
                if cohort is not None and cohort not in req.cohorts():
                    ignored.append({"step": ROLLOUT, "status": "unknown_cohort", **label, "cohort": cohort})
                    continue
                member, matched = req.matcher().evaluate(expr if expr is not None else {"in_cohort": cohort})
            entry: dict[str, Any] = {**label, "cohort": cohort, "match": expr, "member": member,
                                     "cohorts_matched": matched}
            evaluated.append(entry)
            if member is not True:
                continue
            target = {**label, "cohort": cohort, "match": expr, "cohorts_matched": matched, "note": rule.get("note")}
            pct = rule.get("percentage", 100)
            split = rule.get("split")
            unit, uid = req.bucket_subject(flag, rule)
            if (pct < 100 or split) and not uid:
                status = ({"status": "no_subject_to_bucket"} if unit in t.BUCKET_SUBJECTS
                          else {"status": "unit_unknown"})
                ignored.append({"step": ROLLOUT, **status, **label, **({} if unit in t.BUCKET_SUBJECTS
                                                                      else {"unit": unit})})
                continue
            if pct < 100:
                b = t.bucket(flag.key, uid)
                entry.update(percentage=pct, bucket=b, admitted=b < pct, bucket_by=unit)
                if b >= pct:
                    continue
                target = {**target, "percentage": pct, "bucket": b, "bucket_by": unit}
            if split:
                value, position = t.pick_variant(flag.key, uid, split)
                value = flag.normalize(value) or flag.off_value
                return value, {**target, "split_position": position, "bucket_by": unit}
            return flag.normalize(rule.get("value")) or flag.off_value, target
        return None

    def get_flag_metadata(self) -> dict[str, dict[str, Any]]:
        """What a settings panel shows beside each flag."""
        env = get_environment()
        off = self.off_owners()
        return {
            key: {
                "kind": flag.kind or None,
                "variants": dict(flag.variants) if flag.multivariate else None,
                "requires": [{"flag": r.flag, "values": list(r.values)} for r in flag.requires],
                "remove_by": flag.remove_by.isoformat() if flag.remove_by else None,
                "description": flag.description,
                "default": flag.default_for(env),
                "protected": flag.is_protected(env),
                "owner": flag.owner,
                "owner_off": bool(flag.owner) and flag.owner in off,
                "waiting_on": flag.owner if flag.owner and flag.owner in off else None,
            }
            for key, flag in sorted(self.get_registry().items())
        }

    # ------------------------------------------------------------ write

    def set_override(
        self,
        scope: str,
        scope_id: str,
        key: str,
        value: Any,
        *,
        by: str,
        reason: str | None = None,
        expires_at: Any = None,
    ) -> dict[str, Any] | None:
        """Store (or, with ``value=None``, clear) one override, with an optional reason and expiry.

        Refuses an undeclared flag, a value that is not on/off/soon, an expiry that has
        passed, and an unreasoned override of a flag protected in this environment
        (LOOP-F10). A flag whose owning extension is off on this node is stored all the
        same and waits on that extension (:meth:`waiting_on`); it applies the moment the
        extension is on (ADR-254 D3).
        """
        flag = self.get_registry().get(key)
        if flag is None:
            raise FlagError(f"no flag {key!r} is declared", code="unknown")
        if scope not in SCOPES:
            raise FlagError(f"scope is one of {', '.join(SCOPES)}", code="invalid")
        if not scope_id:
            raise FlagError(f"name the {scope}: scope_id is required", code="invalid")
        env = get_environment()
        reason = (reason or "").strip() or None
        if flag.is_protected(env) and not reason:
            raise FlagError(
                f"flag {key!r} is protected in the {env} environment: an override there needs a reason",
                code="protected",
            )
        stored_id = self._sid(scope_id) or scope_id
        if value is None:
            self.overrides.delete(scope, stored_id, key)
            return None
        norm = flag.normalize(value)
        if norm is None:
            spelled = ", ".join(flag.values) if flag.multivariate else "on, off or soon"
            raise FlagError(f"{value!r} is not {spelled}", code="invalid")
        try:
            expiry = t.parse_ts(expires_at)
        except t.RuleError as exc:
            raise FlagError(str(exc), code="invalid") from exc
        if expiry is not None and expiry <= datetime.now(UTC):
            raise FlagError(f"the expiry {expiry.isoformat()} is in the past", code="invalid")
        extra: dict[str, Any] = {}
        if flag.multivariate:  # the value column holds on/off/soon; the variant rides beside it
            norm, extra = ON, {"variant": norm}
        if reason is None and expiry is None and not extra:
            return self.overrides.put(scope, stored_id, key, norm, by=by)
        return self.overrides.put(scope, stored_id, key, norm, by=by, reason=reason, expires_at=expiry, **extra)

    def waiting_on(self, key: str) -> str | None:
        """The off extension ``key`` waits on here, or ``None``."""
        flag = self.get_registry().get(key)
        if flag is None or not flag.owner:
            return None
        return flag.owner if flag.owner in self.off_owners() else None


__all__ = [
    "EXTENSION_OFF",
    "KILL_SWITCH",
    "NO_DATABASE",
    "ORDER",
    "PREREQUISITE",
    "ROLLOUT",
    "CohortMatcher",
    "Decision",
    "FlagError",
    "FlagService",
    "NoOverrides",
    "NoTargeting",
    "OverrideReader",
    "Resolution",
    "TargetingReader",
    "env_override",
    "set_switched_off_owners",
    "switched_off_owners",
    "waiting_text",
]
