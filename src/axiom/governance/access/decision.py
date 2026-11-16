# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The one decision point: may this person do this verb on this object? (ADR-263 D9)

Two answers, from one table (:mod:`.verbs`) and one record (:mod:`.store`):

- :func:`role_of` / :func:`roles_of`: the person's role on an object, the
  highest of their own entry, their groups' entries, the general access line
  and every enclosing folder of a person's (SHR-R1.3). A pure lookup: listings
  and retrieval filter with it, one query for a whole page.
- :func:`require`: the verb's verdict. It is a policy source in GUARD's combiner
  (:class:`AccessSource`), so every answer, yes or no, is a ``decide()``
  receipt, and a site rule can refuse more but never permit what this refuses
  (deny-overrides). No role at all is :class:`NoAccess` (reads as not found); a
  role too low is :class:`Refused`, naming the role needed (R8).

The object's home supplies what only it knows, as an :class:`AccessObject`: who
owns it (the Owner stays where it is today, ADR-263 D10), which person's folders
enclose it, its general access before sharing existed (``legacy_general``, the
migration's projection), whether it is held for export-control review, and who
deleted it. Nothing here reads another extension's tables.

Administration is not authority over content (ADR-142 rule 8, ADR-263 D7): no
site role, ``site-admin`` included, adds a role on an object. Site roles answer
only the verbs that are about the site (``library.book.category``).
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from axiom.governance.access import store
from axiom.governance.access.roles import (
    BOOK_MANAGER,
    FOLDER,
    GROUP_PREFIX,
    RESTRICTED,
    SITE,
    Role,
    best,
    capped,
)
from axiom.governance.access.verbs import Verb, verb

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from axiom.extensions.builtins.authz.decide import DecideContext
    from axiom.governance import ActionEnvelope

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ who and what


@dataclass(frozen=True)
class Who:
    """The person asking, at the site their credential names, with their groups and site roles."""

    principal: str
    site: str
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()

    @property
    def subjects(self) -> tuple[str, ...]:
        return (self.principal, *(f"{GROUP_PREFIX}{g}" for g in self.groups))

    def holds_site_role(self, role: str) -> bool:
        from axiom.governance.roles import canonical_roles

        held = set(canonical_roles(self.roles))
        if role == BOOK_MANAGER:
            # Site-admins hold Book manager by default (SHR-R4.4).
            from axiom.governance.roles import SITE_ADMIN

            return BOOK_MANAGER in held or SITE_ADMIN in held
        return role in held


@dataclass(frozen=True)
class AccessObject:
    """A document or folder, as its home describes it to the decision point."""

    kind: str
    id: str
    site: str
    owner: str
    #: General access when no settings row exists: the home's state before sharing.
    legacy_general: str = SITE
    legacy_general_role: Role = Role.VIEWER
    #: The person's folders that enclose it, nearest first; each passes its role down.
    parents: tuple[AccessObject, ...] = ()
    #: Held for export-control review: its shares are kept and not used (SHR-R8.2).
    held: bool = False
    deleted_by: str = ""
    #: The confirmed export-control label (``public`` .. ``controlled``), or ``None``. A share
    #: never reaches a person whose own clearance does not cover it (ADR-263 D8, SHR-R8.1).
    label: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.id)

    @property
    def ref(self) -> str:
        return f"{self.kind}://{self.id}"


# ----------------------------------------------------------- groups and site roles

_groups_resolver: Callable[[str, str], Iterable[str]] | None = None
_roles_resolver: Callable[[str, str], Iterable[str]] | None = None


def set_resolvers(*, groups: Callable[[str, str], Iterable[str]] | None = None,
                  roles: Callable[[str, str], Iterable[str]] | None = None) -> None:
    """Bind how a person's groups and site roles are found (tests, or a site's own directory)."""
    global _groups_resolver, _roles_resolver
    _groups_resolver, _roles_resolver = groups, roles


def _directory_groups(principal: str) -> list[str]:
    """The person's groups from the configured directory (ADR-103); none when there is no directory."""
    try:
        from axiom.extensions.builtins.directory import PrincipalRef
        from axiom.extensions.builtins.directory.config import load_directory_config

        cfg = load_directory_config()
        if cfg.provider is None:
            return []
        return [g.id for g in cfg.provider.groups_for(PrincipalRef(subject=principal, provider=cfg.provider_name))]
    except Exception as exc:  # noqa: BLE001 - an unreadable directory grants nothing, never everything
        log.warning("access: groups of %s could not be read: %s", principal, type(exc).__name__)
        return []


def _directory_roles(principal: str, groups: Iterable[str]) -> list[str]:
    try:
        from axiom.extensions.builtins.directory.config import load_directory_config

        cfg = load_directory_config()
        if cfg.provider is None:
            return []
        return list(cfg.role_map.roles_for(list(groups)))
    except Exception:  # noqa: BLE001
        return []


def who(principal: str, site: str, *, groups: Iterable[str] | None = None,
        roles: Iterable[str] | None = None) -> Who:
    """The :class:`Who` for a verified caller; groups and roles are looked up when not given."""
    principal = (principal or "").strip()
    if not principal:
        raise ValueError("an access decision needs a person")
    if groups is None:
        groups = _groups_resolver(principal, site) if _groups_resolver else _directory_groups(principal)
    group_ids = tuple(dict.fromkeys(str(g) for g in groups if str(g).strip()))
    found = list(roles or ())
    found += list(_roles_resolver(principal, site) if _roles_resolver else _directory_roles(principal, group_ids))
    return Who(principal=principal, site=site, groups=group_ids, roles=tuple(dict.fromkeys(found)))


# ------------------------------------------------------------------------- roles


def _general(settings: store.Settings | None, obj: AccessObject) -> tuple[str, Role]:
    if settings is not None:
        return settings.general, settings.general_role
    return obj.legacy_general, obj.legacy_general_role


def settings_for(obj: AccessObject, session: Any | None = None) -> store.Settings:
    """The object's general access and settings: its row, else its home's projection."""
    def read(s: Any) -> store.Settings:
        recorded = store.settings_of(s, obj.kind, obj.id)
        if recorded is not None:
            return recorded
        return store.Settings(general=obj.legacy_general, general_role=obj.legacy_general_role, recorded=False)

    if session is not None:
        return read(session)
    with store.session_scope() as s:
        return read(s)


def roles_of(person: Who, objects: Iterable[AccessObject], *, session: Any | None = None) -> dict[tuple[str, str], Role | None]:
    """Each object's role for ``person``, in one read of the record."""
    objs = list(objects)
    if not objs:
        return {}

    def compute(s: Any) -> dict[tuple[str, str], Role | None]:
        pairs: list[tuple[str, str]] = []
        for o in objs:
            pairs.append(o.key)
            pairs += [p.key for p in o.parents]
        grants: dict[tuple[str, str], list[Role]] = {}
        for g in store.grants_on(s, pairs, subjects=person.subjects):
            grants.setdefault((g.kind, g.object_id), []).append(g.role)
        settings = store.settings_many(s, pairs)
        out: dict[tuple[str, str], Role | None] = {}
        for o in objs:
            out[o.key] = _role(person, o, grants, settings)
        return out

    if session is not None:
        return compute(session)
    with store.session_scope() as s:
        return compute(s)


def _own_role(person: Who, obj: AccessObject, grants: dict, settings: dict) -> Role | None:
    """The role ``obj`` itself gives: owner, the person's and groups' entries, its general access."""
    if obj.site != person.site:
        return None
    if obj.owner and obj.owner == person.principal:
        return Role.OWNER
    found: list[Role] = []
    if not obj.held:
        found += grants.get(obj.key, [])
    general, general_role = _general(settings.get(obj.key), obj)
    if general == SITE:
        found.append(general_role)
    return best(found)


def cleared(person: Who, obj: AccessObject) -> bool:
    """Whether the person's own clearance covers the object's confirmed label, asked now, never cached.

    A group share names no one's clearance, and a clearance can be lowered after a
    share, so this is checked every time a share is used. Doubt is no: an
    unreadable label or clearance withholds.
    """
    if not obj.label:
        return True
    try:
        from axiom.governance.classification import clearance_for, parse_label, permits

        label = parse_label(obj.label)
        return label is not None and permits(clearance_for(person.principal), label)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _role(person: Who, obj: AccessObject, grants: dict, settings: dict) -> Role | None:
    if obj.site != person.site:
        return None
    if obj.owner and obj.owner == person.principal:
        return Role.OWNER  # a person's own upload stays theirs; the label gates retrieval and export
    if not cleared(person, obj):
        # Clearance outranks every share: direct, group, folder and general access alike.
        return None
    found = [_own_role(person, obj, grants, settings)]
    for parent in obj.parents:
        inherited = _own_role(person, parent, grants, settings)
        if obj.held and inherited is not None and inherited is not Role.OWNER:
            # A held item's shares are suspended, and a folder's share is one of them.
            parent_general, parent_role = _general(settings.get(parent.key), parent)
            inherited = parent_role if parent_general == SITE else None
        # Owning a folder does not own what someone else filed in it (Google's rule).
        found.append(capped(inherited, Role.EDITOR))
    return best(found)


def role_of(person: Who, obj: AccessObject, *, session: Any | None = None) -> Role | None:
    return roles_of(person, [obj], session=session)[obj.key]


# ---------------------------------------------------------------------- verdicts


class AccessError(PermissionError):
    """Base of the decision point's refusals; carries the receipt that recorded it."""

    def __init__(self, message: str, *, receipt_id: str = "") -> None:
        super().__init__(message)
        self.receipt_id = receipt_id


class NoAccess(AccessError):
    """No role at all: the object reads as absent, and the address reveals nothing (SHR-R6.1)."""


class Refused(AccessError):
    """A role, but too low (or a rule against it): says what is needed and how to ask."""

    def __init__(self, message: str, *, role: Role | None, needed: str, receipt_id: str = "") -> None:
        super().__init__(message, receipt_id=receipt_id)
        self.role = role
        self.needed = needed


@dataclass(frozen=True)
class Decision:
    verb: str
    role: Role | None
    needed: str
    receipt_id: str


@dataclass
class _Pending:
    permit: bool
    reason: str


_PENDING: contextvars.ContextVar[dict[str, _Pending] | None] = contextvars.ContextVar(
    "axiom_access_pending", default=None
)


@dataclass
class AccessSource:
    """The access verdict as a GUARD policy source (ADR-263 D9; ADR-083 registry).

    The verdict is computed by :func:`require` from the record and handed over
    under the envelope's ``dedup_key``; this source only reports it. An envelope it
    was not given is not its business (abstain).
    """

    name: str = "access"
    priority: int = 75
    may_deny: bool = True

    def evaluate(self, envelope: ActionEnvelope, ctx: DecideContext, now: datetime):
        from axiom.extensions.builtins.authz.policy import ABSTAIN, PolicyEffect, PolicyOpinion

        pending = (_PENDING.get() or {}).get(envelope.dedup_key)
        if pending is None:
            return ABSTAIN
        effect = PolicyEffect.PERMIT if pending.permit else PolicyEffect.DENY
        return PolicyOpinion(effect, pending.reason, ("access",))


def build_access_context(*, session_factory: Any | None = None) -> DecideContext:
    """A decide() context whose policy is the site's rules and the access source, nothing that permits by default."""
    from axiom.extensions.builtins.authz.decide import DecideContext
    from axiom.extensions.builtins.authz.policy import PolicySourceRegistry, RuleEngineSource

    return DecideContext(
        session_factory=session_factory,
        policy_sources=PolicySourceRegistry([RuleEngineSource(), AccessSource()]),
    )


_CTX: DecideContext | None = None


def set_decide_context(ctx: DecideContext | None) -> None:
    """Bind the receipt context (tests); ``None`` rebuilds the process default."""
    global _CTX
    _CTX = ctx


def decide_context() -> DecideContext:
    global _CTX
    if _CTX is None:
        from axiom.infra.authority import (
            _apply_site_rules,
            _receipt_session_factory,
            _receipt_table_usable,
        )

        ctx = build_access_context(session_factory=_receipt_session_factory())
        ctx.receipt_table_usable = _receipt_table_usable
        _apply_site_rules(ctx)
        _CTX = ctx
    return _CTX


def _envelope(person: Who, v: Verb, obj: AccessObject, surface: str) -> ActionEnvelope:
    from axiom.governance.capability import CapabilityToken
    from axiom.governance.classification import Classification
    from axiom.governance.envelope import ActionEnvelope
    from axiom.governance.intent import ActionIntent
    from axiom.governance.provenance import ProvenanceRef
    from axiom.governance.resource import ResourceRef
    from axiom.infra.authority import _principal_from_handle

    actor = _principal_from_handle(person.principal)
    intent = ActionIntent(v.name)
    resource = ResourceRef(scheme=obj.kind, identifier=obj.id)
    cls = Classification.INTERNAL
    return ActionEnvelope(
        actor=actor,
        capability=CapabilityToken.for_call(
            issuer=_principal_from_handle("@authority:access"), subject=actor, intent=intent,
            resource=resource, classification=cls,
        ),
        classification=cls,
        context_fragment_id=f"site://{obj.site}",
        provenance_parent=ProvenanceRef.synthetic(f"access:{v.name}"),
        federation_origin=None,
        intent=intent,
        resource=resource,
        deadline=None,
        dedup_key=f"access-{uuid.uuid4().hex}",
        surface=(surface or "").strip(),
        strict=False,
    )


def _record(person: Who, v: Verb, obj: AccessObject, *, permit: bool, reason: str, surface: str) -> tuple[bool, str, str]:
    """Write the verdict as a decide() receipt. Returns (permitted, reason, receipt id)."""
    from axiom.extensions.builtins.authz.decide import decide
    from axiom.governance.verdict import Decision as GuardDecision

    envelope = _envelope(person, v, obj, surface)
    pending = dict(_PENDING.get() or {})
    pending[envelope.dedup_key] = _Pending(permit=permit, reason=reason)
    token = _PENDING.set(pending)
    try:
        verdict = decide(envelope, decide_context())
    finally:
        _PENDING.reset(token)
    allowed = verdict.decision is GuardDecision.PERMIT
    return allowed, (verdict.reason or reason), verdict.receipt_fragment_id


def _needed_for(v: Verb, obj: AccessObject, settings: store.Settings) -> Role | None:
    if v.rule == "download" and not settings.viewers_can_download:
        return Role.EDITOR
    if v.rule == "share" and not settings.editors_can_share:
        return Role.OWNER
    return v.least


def _refusal(v: Verb, role: Role | None, needed: str, settings: store.Settings) -> str:
    have = f"You can {'view' if role is Role.VIEWER else role.value if role else 'not open'} this"
    if v.rule == "share" and not settings.editors_can_share and role is Role.EDITOR:
        return "The owner has turned off sharing by editors; ask the owner."
    if v.rule == "download" and not settings.viewers_can_download and role in (Role.VIEWER, Role.COMMENTER):
        return "The owner has turned off download and copy for viewers; ask the owner."
    return f"{have}. To {v.act} you need {needed}; ask the owner for {needed.lower()} access."


def require(person: Who, verb_name: str, obj: AccessObject, *, session: Any | None = None,
            surface: str = "") -> Decision:
    """Permit ``verb_name`` on ``obj`` for ``person``, or raise; either way a receipt is written."""
    v = verb(verb_name)
    if obj.kind not in v.kinds:
        raise ValueError(f"{v.name} is not a verb on a {obj.kind}")

    def compute(s: Any) -> tuple[Role | None, store.Settings, dict[tuple[str, str], Role | None]]:
        roles = roles_of(person, [obj, *obj.parents], session=s)
        return roles[obj.key], settings_for(obj, s), roles

    if session is not None:
        role, settings, roles = compute(session)
    else:
        with store.session_scope() as s:
            role, settings, roles = compute(s)

    permit, needed, reason = _judge(person, v, obj, role, settings, roles)
    allowed, why, receipt = _record(person, v, obj, permit=permit, reason=reason, surface=surface)
    if allowed:
        return Decision(verb=v.name, role=role, needed=needed, receipt_id=receipt)
    if permit:
        # The access source said yes and something above it said no: a site rule, or no receipt.
        raise Refused(f"{v.act.capitalize()} was refused: {why}", role=role, needed=needed, receipt_id=receipt)
    if role is None and v.rule not in ("at-site", "book-manager"):
        raise NoAccess("You need access. Ask for it, and the owner is told.", receipt_id=receipt)
    raise Refused(reason, role=role, needed=needed, receipt_id=receipt)


def _judge(person: Who, v: Verb, obj: AccessObject, role: Role | None, settings: store.Settings,
           roles: dict[tuple[str, str], Role | None]) -> tuple[bool, str, str]:
    """(permit, the role or site role needed, the reason) for one verb."""
    if obj.site != person.site:
        return False, "access at this site", "not at this object's site"
    if v.rule == "at-site":
        return True, "being at the site", "at the site"
    if v.rule == "book-manager":
        if person.holds_site_role(BOOK_MANAGER):
            return True, "Book manager", "holds Book manager"
        return False, "Book manager", "Only a Book manager changes a Data Book category for the site."
    if v.rule == "delete":
        if role is Role.OWNER:
            return True, "Owner", "owner"
        if any((roles.get(p.key) or Role.VIEWER).at_least(Role.EDITOR) and p.kind == FOLDER for p in obj.parents):
            return True, "Editor of its folder", "editor of the folder that holds it"
        return False, "Owner", _refusal(v, role, "Owner", settings) if role else "no access"
    if v.rule == "restore":
        if role is Role.OWNER or (obj.deleted_by and obj.deleted_by == person.principal and role is not None):
            return True, "Owner", "owner or the person who deleted it"
        return False, "Owner", _refusal(v, role, "Owner", settings) if role else "no access"
    needed = _needed_for(v, obj, settings)
    assert needed is not None
    if role is not None and role.at_least(needed):
        return True, needed.label, f"{role.label} may {v.act}"
    return False, needed.label, _refusal(v, role, needed.label, settings) if role else "no access"


def may(person: Who, verb_name: str, obj: AccessObject, *, session: Any | None = None) -> bool:
    """The verdict without a receipt, for a page deciding which controls to enable (SHR-R9.4)."""
    v = verb(verb_name)
    roles = roles_of(person, [obj, *obj.parents], session=session)
    permit, _needed, _reason = _judge(person, v, obj, roles[obj.key], settings_for(obj, session), roles)
    return permit


def restricted(obj: AccessObject, session: Any | None = None) -> bool:
    return settings_for(obj, session).general == RESTRICTED


__all__ = [
    "AccessError",
    "AccessObject",
    "AccessSource",
    "Decision",
    "NoAccess",
    "Refused",
    "Who",
    "build_access_context",
    "decide_context",
    "may",
    "require",
    "restricted",
    "role_of",
    "roles_of",
    "set_decide_context",
    "set_resolvers",
    "settings_for",
    "who",
]

