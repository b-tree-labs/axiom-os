# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One registry of doors, one check, one read record (ADR-251 D8 and D9; unit LD-1).

A **door** is any surface that serves records or their projections: an HTTP route, a skill
reached from the CLI, MCP or chat, a listing such as a timeline or a search provider, an
export. Every door is registered here as data (:class:`Door`), and every door asks
:func:`check` the same question before it serves anything. The cross-door conformance matrix
enumerates :func:`doors` and :class:`Reason`, so a door registered without its rows, or a
route that serves records without being registered, fails the build.

**The one check** answers, in this order, and stops at the first refusal:

1. ``unresolved_caller``: nobody can be named for the read.
2. ``site``: the target's site is outside the caller's site scope. Nothing about that site is
   said: the refusal names no site and carries no count (J1's hidden-count rule).
3. ``policy_unavailable``: GUARD, the one decision function, could not decide the read (an
   outage, an error or an unwritable receipt). A policy error never allows.
4. ``not_permitted`` or ``revoked``: GUARD refused the read, or the home's own grant rule
   (``set_rule``) did. ``revoked`` is a grant that once allowed the read and was withdrawn.
5. ``unlabeled``: the target carries no labels, or labels outside the vocabulary. Unlabeled is
   never public (the memory serving gate's rule, made general).
6. ``classification``: the target's classification is above the caller's ceiling.
7. ``export_control``: the target is export-controlled and the caller has no clearance for it.

**One answer per reason.** :data:`WIRE` maps each reason to the one status and code every door
and every transport gives. ``not_permitted``, ``revoked`` and ``unlabeled`` all answer "not
found": a log a person may not read does not exist for them, and a different answer for each
would hint at what is hidden. The exact reason is in the read record.

**One read record.** :func:`access_fields` is the one shape a door records for every allow and
every deny: principal, door, surface, transport, site, scope, record ids or a count, the labels
at the time, and the refusal. No field holds content.

A home (an extension) supplies only what it alone knows: its doors, its grant rule
(:func:`set_rule`) and its labels. Axiom names no domain here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from axiom.governance.classification import Classification, clearance_for, parse_label
from axiom.memory.serving import DenyReason, PolicyUnavailable

log = logging.getLogger("axiom.governance.doors")

# -- the vocabulary -------------------------------------------------------------------------


class Reason(str, Enum):
    """Every reason a door refuses a read. The one vocabulary; no door has its own."""

    UNRESOLVED_CALLER = "unresolved_caller"
    SITE = "site"
    POLICY_UNAVAILABLE = DenyReason.POLICY_UNAVAILABLE.value
    NOT_PERMITTED = "not_permitted"
    REVOKED = "revoked"
    UNLABELED = DenyReason.UNLABELED.value
    CLASSIFICATION = "classification"
    EXPORT_CONTROL = "export_control"


#: The order the check asks in. A reason earlier in the list is answered before a later one.
ORDER: tuple[Reason, ...] = tuple(Reason)

#: The one answer per reason, ``(status, code)``, for every door and every transport.
WIRE: Mapping[Reason, tuple[int, str]] = {
    Reason.UNRESOLVED_CALLER: (401, "no_session"),
    Reason.SITE: (403, "site"),
    Reason.POLICY_UNAVAILABLE: (503, "policy_unavailable"),
    Reason.NOT_PERMITTED: (404, "not_found"),
    Reason.REVOKED: (404, "not_found"),
    Reason.UNLABELED: (404, "not_found"),
    Reason.CLASSIFICATION: (403, "classification"),
    Reason.EXPORT_CONTROL: (403, "export_control"),
}

#: What the person is told, per reason. None names a site, a log or a count.
DETAIL: Mapping[Reason, str] = {
    Reason.UNRESOLVED_CALLER: "sign in first",
    Reason.SITE: "this sign-in reads only its own site",
    Reason.POLICY_UNAVAILABLE: "this read cannot be decided right now; nothing was read, try again",
    Reason.NOT_PERMITTED: "not found",
    Reason.REVOKED: "not found",
    Reason.UNLABELED: "not found",
    Reason.CLASSIFICATION: "this is labelled above what you may read",
    Reason.EXPORT_CONTROL: "this is export-controlled and you hold no clearance for it",
}

#: Reasons a listing never counts: the caller has no right to know that place has activity.
NEVER_COUNTED = frozenset({Reason.UNRESOLVED_CALLER, Reason.SITE, Reason.NOT_PERMITTED,
                           Reason.REVOKED, Reason.UNLABELED})  # fmt: skip

#: Where records are served. Every surface either has doors or says why it has none.
SURFACES = ("api", "mcp", "cli", "chat", "timeline", "search", "export", "charts", "reports",
            "backup", "federation")  # fmt: skip

#: Transports a door is reached by.
HTTP = "http"
SKILL_TRANSPORTS = ("cli", "mcp", "agent_tool")
TRANSPORTS = (HTTP, *SKILL_TRANSPORTS)

#: The labels every record carries. A target without them is ``unlabeled``.
LABEL_KEYS = ("classification", "export_controlled")


# -- doors ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Door:
    """One way records leave the platform. Data, so a later unit adds a door or a parameter as
    a row: the matrix picks it up without a second check."""

    id: str
    extension: str
    #: Which of :data:`SURFACES` this door belongs to.
    surface: str
    #: What it serves, as the read record names it (``records``, ``record``, ``export``...).
    serves: str
    #: ``(method, path)`` for an HTTP door.
    route: tuple[str, str] | None = None
    #: The skill name for a skill door.
    skill: str | None = None
    transports: tuple[str, ...] = (HTTP,)
    #: The inputs that narrow the read. A parameter added here is a row in the matrix.
    params: tuple[str, ...] = ()
    #: ``target``: one scope is asked for and the whole answer is refused; ``listing``: many
    #: scopes are read and a refused one is absent (and, for :data:`NEVER_COUNTED`, uncounted).
    shape: str = "target"
    #: Which input names the scope (the log) and which names a record, for a matrix driver.
    scope_param: str | None = "logbook"
    record_param: str | None = None

    def __post_init__(self) -> None:
        if self.surface not in SURFACES:
            raise ValueError(f"door {self.id!r}: surface {self.surface!r} is not one of {SURFACES}")
        if self.shape not in ("target", "listing"):
            raise ValueError(f"door {self.id!r}: shape is target or listing")
        bad = set(self.transports) - set(TRANSPORTS)
        if bad:
            raise ValueError(f"door {self.id!r}: unknown transports {sorted(bad)}")
        if HTTP in self.transports and not self.route:
            raise ValueError(f"door {self.id!r}: an HTTP door names its route")
        if set(self.transports) & set(SKILL_TRANSPORTS) and not self.skill:
            raise ValueError(f"door {self.id!r}: a skill door names its skill")


_doors: dict[str, Door] = {}
_no_doors: dict[str, str] = {}
_not_doors: dict[tuple[str, str], str] = {}
_lock = threading.Lock()


def register(*doors: Door) -> None:
    """Add doors. Registering the same door again is a no-op; a different door under a taken id
    is refused."""
    with _lock:
        for d in doors:
            held = _doors.get(d.id)
            if held is not None and held != d:
                raise ValueError(f"door {d.id!r} is already registered differently")
            _doors[d.id] = d


def add_param(door_id: str, *params: str) -> Door:
    """A later unit's narrowing input (a field filter, say) added to a door as data."""
    from dataclasses import replace

    with _lock:
        d = _doors[door_id]
        d = replace(d, params=tuple(dict.fromkeys((*d.params, *params))))
        _doors[door_id] = d
        return d


def declare_no_door(surface: str, why: str) -> None:
    """A surface that serves no records today, and why. The matrix holds it to that."""
    if surface not in SURFACES:
        raise ValueError(f"{surface!r} is not one of {SURFACES}")
    _no_doors[surface] = why


def declare_not_a_door(method: str, path: str, why: str) -> None:
    """A route in a home that serves records which is not itself a door (a write, a draft, a
    declaration), and why. The router walk accepts it only with a reason."""
    _not_doors[(method.upper(), path)] = why


def doors(*, extension: str | None = None) -> tuple[Door, ...]:
    return tuple(d for d in sorted(_doors.values(), key=lambda d: d.id)
                 if extension is None or d.extension == extension)  # fmt: skip


def get(door_id: str) -> Door:
    try:
        return _doors[door_id]
    except KeyError:
        raise KeyError(f"no door {door_id!r} is registered") from None


def by_route(method: str, path: str) -> Door | None:
    for d in _doors.values():
        if d.route and d.route[0] == method.upper() and d.route[1] == path:
            return d
    return None


def by_skill(name: str) -> Door | None:
    for d in _doors.values():
        if d.skill == name:
            return d
    return None


def no_door_surfaces() -> Mapping[str, str]:
    return dict(_no_doors)


def not_doors() -> Mapping[tuple[str, str], str]:
    return dict(_not_doors)


# -- who asks and what for ------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who asks. ``sites`` None is unbounded (an operator on a node with no bound)."""

    handle: str | None
    sites: frozenset[str] | None = None
    ceiling: str = Classification.INTERNAL.value
    #: Export-control clearance; None reads it from the directory (``clearance_for``).
    clearance: str | None = None

    @property
    def resolved(self) -> bool:
        return bool((self.handle or "").strip())

    def may_see_site(self, site: str | None) -> bool:
        return site is None or self.sites is None or site in self.sites


@dataclass(frozen=True)
class Target:
    """What is asked for: a site, a scope within it (a log), the labels it carries, and the
    record ids when one is named. ``labels`` None is unlabeled."""

    site: str | None
    scope: str | None = None
    labels: Mapping[str, Any] | None = None
    ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Reason | None = None
    detail: str = ""
    labels: Mapping[str, Any] | None = None

    @property
    def status(self) -> int:
        return 200 if self.allowed else WIRE[self.reason][0]  # type: ignore[index]

    @property
    def code(self) -> str | None:
        return None if self.allowed else WIRE[self.reason][1]  # type: ignore[index]

    def body(self) -> dict[str, str]:
        """The refusal every door sends: a reason and a sentence, nothing of any record."""
        return {"detail": DETAIL[self.reason], "code": self.code}  # type: ignore[index]


ALLOW = "allow"
DENY = "deny"
REVOKED = "revoked"

#: ``(caller, door, target) -> "allow" | "deny" | "revoked"``: a home's own grant rule.
Rule = Callable[[Caller, Door, Target], str]
#: ``(caller, door, target) -> bool``: GUARD's answer; raises when it cannot answer.
Policy = Callable[[Caller, Door, Target], bool]

_rules: dict[str, Rule] = {}


def set_rule(extension: str, rule: Rule | None) -> None:
    """The home's grant rule (roles, sharing, per-row resolvers), composed into the one check."""
    if rule is None:
        _rules.pop(extension, None)
    else:
        _rules[extension] = rule


def guard_policy(caller: Caller, door: Door, target: Target) -> bool:
    """GUARD's answer for a read of the target's site: the one decision function every read
    asks (``data.read`` on ``site://<site>``), open by default, closed by a site's rules. An
    error, an outage or an unwritten receipt raises :class:`PolicyUnavailable`."""
    if not target.site:
        return True
    from axiom.infra import site_authority as sa

    try:
        envelope = sa.build_site_read_envelope(target.site, str(caller.handle), surface=door.surface)
        verdict = sa.decide_site_read(envelope)
    except Exception as exc:  # noqa: BLE001 - nothing falls open
        raise PolicyUnavailable(f"GUARD could not decide: {type(exc).__name__}") from exc
    reason = str(getattr(verdict, "reason", "") or "")
    try:
        from axiom.extensions.builtins.authz.decide import RECEIPT_WRITE_FAILED
    except Exception:  # noqa: BLE001
        RECEIPT_WRITE_FAILED = "receipt write failed"  # noqa: N806
    if reason == RECEIPT_WRITE_FAILED:
        raise PolicyUnavailable("GUARD could not record its decision")
    disposition = getattr(verdict, "disposition", verdict)
    return "permit" in str(getattr(disposition, "value", disposition)).lower()


_policy: Policy = guard_policy


def set_policy(policy: Policy | None) -> None:
    """Replace GUARD's onramp (a test seam, and the place a federation peer's policy plugs in)."""
    global _policy
    _policy = policy or guard_policy


def _clearance(caller: Caller) -> Classification:
    if caller.clearance:
        got = parse_label(caller.clearance)
        if got is not None:
            return got
    return clearance_for(caller.handle)


def labels_valid(labels: Mapping[str, Any] | None) -> bool:
    if not labels or any(k not in labels for k in LABEL_KEYS):
        return False
    return parse_label(str(labels["classification"] or "")) is not None and isinstance(
        labels["export_controlled"], bool
    )


def check(door: Door | str, caller: Caller, target: Target) -> Decision:
    """The one check. Never raises: anything unexpected is ``policy_unavailable``."""
    d = get(door) if isinstance(door, str) else door
    try:
        return _check(d, caller, target)
    except PolicyUnavailable as exc:
        return Decision(False, Reason.POLICY_UNAVAILABLE, str(exc), target.labels)
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.warning("door %s: the check failed: %s", d.id, exc)
        return Decision(False, Reason.POLICY_UNAVAILABLE, type(exc).__name__, target.labels)


def _check(door: Door, caller: Caller, target: Target) -> Decision:
    labels = target.labels
    if not caller.resolved:
        return Decision(False, Reason.UNRESOLVED_CALLER, "", labels)
    if not caller.may_see_site(target.site):
        # Nothing about the site travels: no labels, no ids, no count.
        return Decision(False, Reason.SITE, "", None)
    if not _policy(caller, door, target):
        return Decision(False, Reason.NOT_PERMITTED, "GUARD refused", labels)
    rule = _rules.get(door.extension)
    if rule is not None:
        verdict = rule(caller, door, target)
        if verdict == REVOKED:
            return Decision(False, Reason.REVOKED, "", labels)
        if verdict != ALLOW:
            return Decision(False, Reason.NOT_PERMITTED, "", labels)
    if not labels_valid(labels):
        return Decision(False, Reason.UNLABELED, "", labels)
    label = parse_label(str(labels["classification"]))  # type: ignore[index]
    ceiling = parse_label(caller.ceiling) or Classification.PUBLIC
    if label is None or label.tier > ceiling.tier:
        return Decision(False, Reason.CLASSIFICATION, "", labels)
    if labels["export_controlled"] and _clearance(caller).tier < Classification.CONTROLLED.tier:  # type: ignore[index]
        return Decision(False, Reason.EXPORT_CONTROL, "", labels)
    return Decision(True, None, "", labels)


# -- a door refused before it is reached --------------------------------------------------

#: ``(skill, params, ctx) -> refusal | None``: a home's answer for its door when a platform
#: pre-check (a tool-call hook) refused the call before the door ran.
Refuser = Callable[[str, dict, Any], Any]
_refusers: dict[str, Refuser] = {}


def set_refuser(extension: str, refuser: Refuser | None) -> None:
    if refuser is None:
        _refusers.pop(extension, None)
    else:
        _refusers[extension] = refuser


def refusal_at_dispatch(skill: str, params: dict | None, ctx: Any) -> Any:
    """The door's own refusal for a skill door a pre-check refused, or None when the skill is
    not a door. Never raises: a failure here leaves the pre-check's own refusal standing."""
    door = by_skill(skill)
    if door is None or door.extension not in _refusers:
        return None
    try:
        return _refusers[door.extension](skill, dict(params or {}), ctx)
    except Exception as exc:  # noqa: BLE001
        log.warning("door %s: no refusal at dispatch: %s", door.id, exc)
        return None


# -- objects served only through a door -----------------------------------------------------

#: A database object that holds record content or a projection of it carries this mark in its
#: comment, and is served only through its doors: a generic reader (a gold verb, a table
#: browser) answers it as an object that does not exist and does not list it. The mark lives in
#: the database, so it holds whether or not the home that made the object is loaded.
DOOR_ONLY_MARK = "axiom:door-only"


def door_only_comment(door_ids: Iterable[str], why: str) -> str:
    """The comment that marks an object door-only, naming the doors that serve it."""
    return f"{DOOR_ONLY_MARK} ({', '.join(door_ids)}): {why}"


def not_door_only_sql(schema_col: str, name_col: str) -> str:
    """A SQL predicate, true for an object that is NOT door-only (for catalogue queries)."""
    return (f"coalesce(obj_description(to_regclass(quote_ident({schema_col}) || '.' || "
            f"quote_ident({name_col})), 'pg_class'), '') NOT LIKE '{DOOR_ONLY_MARK}%%'")  # fmt: skip


# -- the read record ------------------------------------------------------------------------

#: The fields a read record's ``access`` may hold. Anything else is refused, so content
#: cannot reach the record by accident.
#: ``source`` names where a read came from when it was not the live store: an archived period's
#: package (``archive:sha256:<package digest>``, unit LD-5). Never content.
ACCESS_FIELDS = ("what", "door", "surface", "transport", "logbook", "site", "ids", "count",
                 "person", "labels", "refused", "reason", "status", "source")  # fmt: skip


def access_fields(
    *,
    door: Door,
    decision: Decision,
    caller: Caller,
    target: Target,
    transport: str,
    ids: Iterable[str] = (),
    count: int | None = None,
) -> dict[str, Any]:
    """The one shape of a read record, allow or deny. Ids and counts only; on a refusal for a
    site outside scope, not even the site's labels."""
    id_list = [str(i) for i in ids if i] if decision.allowed else []
    out = {
        "what": door.serves,
        "door": door.id,
        "surface": door.surface,
        "transport": transport,
        "logbook": target.scope,
        "site": target.site,
        "ids": id_list,
        "count": (len(id_list) if count is None else int(count)) if decision.allowed else 0,
        "person": caller.handle,
        "labels": dict(decision.labels) if decision.labels else None,
        "refused": decision.code,
        "reason": decision.reason.value if decision.reason else None,
        "status": decision.status,
        "source": None,  # the live store; a door that read an archived package names it
    }
    assert set(out) <= set(ACCESS_FIELDS)
    return out


@dataclass
class Admission:
    """A door's admitted read, carried while the door serves it, so the record it writes names
    the door, the labels and the transport without the door passing them along."""

    door: Door
    caller: Caller
    target: Target
    decision: Decision
    transport: str
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ACCESS_FIELDS",
    "ALLOW",
    "Admission",
    "Caller",
    "DENY",
    "DETAIL",
    "DOOR_ONLY_MARK",
    "Decision",
    "Door",
    "HTTP",
    "LABEL_KEYS",
    "NEVER_COUNTED",
    "ORDER",
    "PolicyUnavailable",
    "REVOKED",
    "Reason",
    "SKILL_TRANSPORTS",
    "SURFACES",
    "TRANSPORTS",
    "Target",
    "WIRE",
    "access_fields",
    "add_param",
    "by_route",
    "by_skill",
    "check",
    "declare_no_door",
    "declare_not_a_door",
    "door_only_comment",
    "doors",
    "get",
    "guard_policy",
    "labels_valid",
    "no_door_surfaces",
    "not_door_only_sql",
    "not_doors",
    "register",
    "refusal_at_dispatch",
    "set_policy",
    "set_refuser",
    "set_rule",
]
