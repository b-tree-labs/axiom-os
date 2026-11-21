# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The log's doors, registered in the one registry and admitted by the one check (unit LD-1).

Every way a signed record, or a projection of one, leaves the log is a :class:`Door` here: the
HTTP routes, the read skills the CLI, MCP and chat reach, the log provider of search and the
Timeline's attestations lane. Each asks :func:`axiom.governance.doors.check` with what only
this home knows:

- **its grant rule** (:func:`rule`): the person's roles at the site read through the log's
  ``readers`` (unit W1-A9); a role that once allowed the read and was revoked is ``revoked``;
- **its labels** (:func:`labels_for`): a record carries its log's declared ``classification``
  and ``export_controlled`` flag (ADR-251 D1); a record of a log with no declaration is
  unlabeled, and unlabeled is never served.

Each answer, allow or deny, writes one read record (:func:`record`) in the shape
:func:`axiom.governance.doors.access_fields` defines. A later unit adds a door or a parameter
here as a row (:func:`axiom.governance.doors.add_param`), never a second check.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.governance import doors as g
from axiom.governance.doors import Caller, Decision, Door, Reason, Target
from axiom.infra import audit_trail

log = logging.getLogger("axiom.attest.doors")

API = "/api/v1/attest"
EXT = "attest"
_LOG = API + "/{logbook}"
_SKILLS = g.SKILL_TRANSPORTS
_FILTERS = ("entry_type", "meaning", "signer", "from", "to", "interval", "q")
#: What the one search over the index narrows by (unit LD-2).
_SEARCH = ("q", "site", "logbook", "since", "until", "entry_type", "signer", "kind", "exclude_kind",
           "interval", "limit")


def _http(id_: str, method: str, path: str, serves: str, **kw: Any) -> Door:
    return Door(id=id_, extension=EXT, surface="api", serves=serves, route=(method, path), **kw)


def _skill(name: str, serves: str, *, surface: str = "mcp", **kw: Any) -> Door:
    return Door(id=name, extension=EXT, surface=surface, serves=serves, skill=name,
                transports=_SKILLS, **kw)  # fmt: skip


DOORS: tuple[Door, ...] = (
    # -- HTTP -------------------------------------------------------------------------------
    _http("attest.http.records", "GET", _LOG + "/records", "records",
          params=(*_FILTERS, "unacknowledged", "cosign", "order")),
    _http("attest.http.record", "GET", _LOG + "/records/{attestation_id}", "record",
          record_param="attestation_id"),
    # An attachment's bytes, by hash, only of a record the caller may read that names it (LD-4).
    _http("attest.http.attachment", "GET", _LOG + "/records/{attestation_id}/attachments/{sha256}",
          "attachment", record_param="attestation_id"),
    _http("attest.http.intervals", "GET", _LOG + "/intervals", "intervals", params=("kind",)),
    # A log's declared filters and the values a reader may pick (ADR-268 D5): read from the field
    # index, a projection of the records, under the same check as the records door.
    _http("attest.http.filters", "GET", _LOG + "/filters", "filters", params=("field", "prefix")),
    _http("attest.http.obligations", "GET", _LOG + "/obligations", "obligations"),
    # The versions of a declared key set, derived from the signed records (#1803): a projection
    # of the records, under the same check as the records door.
    _http("attest.http.versions", "GET", _LOG + "/versions", "versions",
          params=("entry_type", "keys")),
    # The cross-check and predicate facts beside the records (#1816): system facts carrying the
    # log's labels, under the same check as the records door.
    _http("attest.http.crosschecks", "GET", _LOG + "/crosschecks", "crosschecks",
          params=("result", "attestation_id")),
    _http("attest.http.verify", "POST", _LOG + "/verify", "verify", params=("from_seq", "to_seq")),
    _http("attest.http.stream", "GET", _LOG + "/stream", "stream"),
    _http("attest.http.seal_preview", "GET", _LOG + "/seal/preview", "seal"),
    _http("attest.http.seal_due", "GET", _LOG + "/seal/due", "seal"),
    _http("attest.http.imports", "GET", _LOG + "/imports", "imports"),
    # A log's sealed periods archived as packages (unit LD-5): metadata only, by the one check.
    _http("attest.http.archives", "GET", _LOG + "/archives", "archive"),
    # A log's retention: declared, in force and next due (unit LD-6), by the one check; no content.
    _http("attest.http.retention", "GET", _LOG + "/retention", "retention"),
    Door(id="attest.http.export_create", extension=EXT, surface="export", serves="export",
         route=("POST", _LOG + "/exports"), params=_FILTERS),
    Door(id="attest.http.export_preview", extension=EXT, surface="export", serves="export",
         route=("POST", _LOG + "/exports/preview"), params=_FILTERS),
    # One view of a selection streamed as it is read (#2182): no package, no stored export.
    Door(id="attest.http.export_stream", extension=EXT, surface="export", serves="export",
         route=("GET", _LOG + "/exports/stream"), params=(*_FILTERS, "format")),
    Door(id="attest.http.export_list", extension=EXT, surface="export", serves="export",
         route=("GET", _LOG + "/exports")),
    Door(id="attest.http.export_meta", extension=EXT, surface="export", serves="export",
         route=("GET", API + "/exports/{export_id}"), scope_param=None, record_param="export_id"),
    Door(id="attest.http.export_download", extension=EXT, surface="export", serves="export",
         route=("GET", API + "/exports/{export_id}/download"), scope_param=None,
         record_param="export_id"),
    _http("attest.http.decision", "GET", API + "/decisions/{action_id}", "decision",
          scope_param=None, record_param="action_id"),
    # -- skills (CLI, MCP, chat) ------------------------------------------------------------
    _skill("attest.show", "records", params=(*_FILTERS, "limit"), record_param="attestation_id"),
    _skill("attest.intervals", "intervals", params=("kind",)),
    _skill("attest.filters", "filters", params=("field", "prefix")),
    _skill("attest.verify", "verify"),
    _skill("attest.export", "export", surface="export", params=_FILTERS),
    _skill("attest.export_preview", "export", surface="export", params=_FILTERS),
    _skill("attest.unacknowledged", "unacknowledged", params=("by", "since_seq")),
    _skill("attest.cosign_pending", "cosign"),
    _skill("attest.seal_due", "seal"),
    _skill("attest.seal_preview", "seal"),
    _skill("attest.obligations", "obligations"),
    _skill("attest.crosschecks", "crosschecks", params=("result", "attestation_id")),
    _skill("attest.imports", "imports"),
    _skill("attest.archives", "archive"),
    _skill("attest.retention", "retention"),
    # An archived period read again from its package and warmed in the index (unit LD-8). It
    # writes (warm index rows), so it is a terminal verb only: not on MCP, and so not in chat.
    Door(id="attest.rehydrate", extension=EXT, surface="cli", serves="archive", skill="attest.rehydrate",
         transports=("cli",), params=("archive_id",)),
    _skill("attest.access", "access", params=("since", "limit")),
    _skill("attest.archives", "archive"),
    # A memory reference re-resolved: the record as of gold, for the caller now (unit LD-3). Memory
    # recall reaches it outside a skill (axiom.memory.references); chat, MCP and the CLI by skill.
    _skill("attest.reference", "record", record_param="attestation_id"),
    # -- listings ---------------------------------------------------------------------------
    Door(id="search.logbooks", extension=EXT, surface="search", serves="search",
         route=("GET", "/api/v1/search"), shape="listing", params=("q",)),
    Door(id="search.logbooks_suggest", extension=EXT, surface="search", serves="search",
         route=("GET", "/api/v1/search/suggest"), shape="listing", params=("q",)),
    # The one search over the log's index (unit LD-2): chat, MCP and the CLI as a skill, the
    # Timeline by its route; the Log page's search is ``search.logbooks``.
    Door(id="attest.search", extension=EXT, surface="search", serves="search", skill="attest.search",
         transports=_SKILLS, shape="listing", params=_SEARCH),
    Door(id="journal.search", extension=EXT, surface="timeline", serves="search",
         route=("GET", "/api/v1/journal/search"), shape="listing", params=_SEARCH),
    Door(id="journal.attestations", extension=EXT, surface="timeline", serves="timeline",
         route=("GET", "/api/v1/journal"), shape="listing", scope_param=None),
    Door(id="journal.attestations_stream", extension=EXT, surface="timeline", serves="timeline",
         route=("GET", "/api/v1/journal/stream"), shape="listing", scope_param=None),
)

#: Routes under ``/api/v1/attest`` that are not doors, and why (the router walk reads these).
NOT_DOORS: dict[tuple[str, str], str] = {
    ("GET", API + "/logbooks"): "the logs' declarations, filtered by the read rule; no record",
    ("GET", API + "/logbooks/{logbook}"): "a log's declaration; no record",
    ("POST", API + "/drafts"): "a person's own draft; not a signed record",
    ("GET", API + "/drafts"): "a person's own drafts",
    ("GET", API + "/drafts/{draft_id}"): "a person's own draft",
    ("PUT", API + "/drafts/{draft_id}"): "a person's own draft",
    ("DELETE", API + "/drafts/{draft_id}"): "a person's own draft",
    ("POST", API + "/drafts/{draft_id}/present"): "presenting a person's own draft",
    ("POST", API + "/drafts/{draft_id}/attachments"): "attaching a file to a person's own draft",
    ("DELETE", API + "/drafts/{draft_id}/attachments/{sha256}"): "taking a file off a person's own draft",
    ("POST", API + "/proposals"): "software drafts into a tray; reads nothing",
    ("POST", API + "/presentations/{presentation_id}/respond"): "a person's answer",
    ("POST", API + "/sign"): "signing a person's own draft",
    ("GET", API + "/approvals/pending"): "held actions in the gate's queue; not signed records",
    ("POST", API + "/approvals/access-request"): "asking for a role",
    ("POST", API + "/approvals/{action_id}/decision"): "drafting a decision",
    ("GET", _LOG + "/access-request"): "the person's own request for access",
    ("POST", _LOG + "/access-request"): "asking for access",
    ("POST", API + "/access-requests/{request_id}/withdraw"): "a person's own request",
    ("POST", API + "/access-requests/{request_id}/remind"): "a person's own request",
    ("GET", API + "/access-requests"): "requests a decider answers; no record",
    ("POST", API + "/access-requests/{request_id}/answer"): "answering a request",
    ("POST", _LOG + "/records/{attestation_id}/ack"): "drafting an acknowledgement (writes)",
    ("POST", _LOG + "/records/{attestation_id}/cosign"): "drafting a co-signature (writes)",
    ("POST", _LOG + "/records/{attestation_id}/supplement"): "drafting a supplement (writes)",
    ("POST", _LOG + "/seal"): "drafting a close-out (writes)",
    ("POST", _LOG + "/imports/dry-run"): "checks a file the person sends; reads no record",
    ("GET", _LOG + "/forms/{form}/blank"): "a declared form printed blank; no record",
    ("GET", "/api/v1/journal/lanes"): "the lanes a caller may follow; names, no row",
}

g.register(*DOORS)
# The log's declared field filters (ADR-268 D3) narrow every records door: a parameter row on each,
# never a second check. ``field.<id>`` stands for every declared filter's keys (``.contains``,
# ``.gte``, ``.lte``, ``.unit``); ``where`` is the skills' structured spelling.
FIELD_PARAMS = ("field.<id>", "where")
for _door in ("attest.http.records", "attest.http.export_create", "attest.http.export_preview",
              "attest.http.export_stream", "attest.show", "attest.export",
              "attest.export_preview"):  # fmt: skip
    g.add_param(_door, *FIELD_PARAMS)
DOORS = tuple(g.get(d.id) for d in DOORS)
for (_m, _p), _why in NOT_DOORS.items():
    g.declare_not_a_door(_m, _p, _why)
g.declare_no_door("charts", "charts draw signals and gold tables; no chart reads a log's records")
g.declare_no_door("reports", "no report reads a log's records today; a report that does is a door")
g.declare_no_door("backup", "a restore writes the store back; it serves nothing to a caller")
g.declare_no_door("federation", "no peer is sent a log's records; federation carries none today")


# -- the caller -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LogCaller(Caller):
    """A caller with what the log's grant rule reads: the gate roles of a sign-in."""

    gate_roles: tuple[str, ...] = ()
    #: Where the caller's roles are read: the skill's state dir, or the node's.
    state_dir: Path | None = None


def _state_dir() -> Path:
    """The node's state dir, through the API's one seam."""
    from . import api

    return Path(api._state_dir())


def caller_from_session(person: dict[str, Any] | None) -> LogCaller:
    """A browser session: its site, narrowed by what this node serves (journal J1's rule)."""
    from axiom.infra.site_scope import deployment_sites

    if not person or "unnamed" in person:
        return LogCaller(handle=None)
    served = deployment_sites()
    site = person.get("site")
    if site:
        sites: frozenset[str] | None = frozenset({site}) & served if served is not None else frozenset({site})
    else:
        sites = frozenset(served) if served is not None else None
    return LogCaller(handle=str(person["handle"]), sites=sites,
                     gate_roles=tuple(person.get("gate_roles") or ()), state_dir=_state_dir())  # fmt: skip


def _ctx_handle(ctx: Any) -> str | None:
    handle = getattr(getattr(ctx, "principal", None), "handle", None)
    if handle:
        return str(handle)
    if getattr(ctx, "surface", None) in (None, "cli"):
        # A terminal is its operator; the same somebody every site read asks as.
        from axiom.infra.site_authority import _asking_principal

        return _asking_principal()
    return None


def caller_from_ctx(ctx: Any) -> LogCaller:
    """A skill's caller (CLI, MCP, chat): its principal and its credential's site scope."""
    from axiom.infra.site_authority import SiteNotAuthorized
    from axiom.infra.site_scope import resolve

    handle = _ctx_handle(ctx)
    try:
        scope = resolve()
        sites = None if scope.unbounded else frozenset(scope.sites)
    except SiteNotAuthorized:
        sites = frozenset()
    held = getattr(ctx, "state_dir", None)
    return LogCaller(handle=handle, sites=sites, state_dir=Path(held) if held else None)


_request_caller: ContextVar[LogCaller | None] = ContextVar("axiom_attest_request_caller", default=None)


def bind_request_caller(caller: LogCaller):
    """The log's caller for this request, for a listing door reached through another home's
    route (search). Returns the token for :func:`unbind_request_caller`."""
    return _request_caller.set(caller)


def unbind_request_caller(token) -> None:
    _request_caller.reset(token)


def request_caller() -> LogCaller | None:
    return _request_caller.get()


# -- what only the log knows: labels and the grant rule -------------------------------------


def labels_for(logbook: str | None) -> dict[str, Any] | None:
    """The labels every record of the log carries, from its declaration; None when undeclared."""
    from . import registry
    from .logbooks import LogbookError

    if not logbook:
        return None
    try:
        return registry.get(logbook).labels
    except LogbookError:
        return None


def _roles(caller: Caller, site: str) -> tuple[str, ...]:
    from .roles import roles_for

    return roles_for(str(caller.handle), site, state_dir=_dir_of(caller),
                     gate_roles=getattr(caller, "gate_roles", ()))  # fmt: skip


def _dir_of(caller: Caller) -> Path:
    return getattr(caller, "state_dir", None) or _state_dir()


def _revoked(handle: str, site: str, book: Any, state_dir: Path) -> bool:
    """A role that would let ``handle`` read ``book`` was revoked at ``site`` and not re-granted."""
    from .roles import changes_file

    path = changes_file(state_dir)
    if not path.exists():
        return False
    # With no log (a door whose rule is any role at the site), any role revoked there counts.
    reading = None if book is None else set(book.readers) | {r for t in book.types for r in t.roles}
    last: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e.get("principal") == handle and e.get("site") == site:
                if book is not None and e.get("logbook") not in (None, "*", book.id):
                    continue
                last[str(e.get("role"))] = str(e.get("action"))
    except (OSError, ValueError):
        return False
    return any(action == "revoke" and (reading is None or role in reading) for role, action in last.items())


def rule(caller: Caller, door: Door, target: Target) -> str:
    """The log's grant rule (unit W1-A9), composed into the one check."""
    from . import access_rules, registry
    from .logbooks import LogbookError

    if not target.site:
        return g.DENY
    roles = _roles(caller, target.site)
    if door.id == "attest.http.decision":
        # A decision is readable by anyone who holds a role at the site (approvals design Q3).
        if roles:
            return g.ALLOW
        return g.REVOKED if _revoked(str(caller.handle), target.site, None, _dir_of(caller)) else g.DENY
    if not target.scope:
        return g.ALLOW
    try:
        book = registry.get(target.scope)
    except LogbookError:
        return g.ALLOW  # an undeclared log has no labels, and the check refuses it as unlabeled
    if access_rules.can_read(book, roles):
        return g.ALLOW
    return g.REVOKED if _revoked(str(caller.handle), target.site, book, _dir_of(caller)) else g.DENY


g.set_rule(EXT, rule)


def target(site: str | None, logbook: str | None, ids: tuple[str, ...] = ()) -> Target:
    return Target(site=site, scope=logbook, labels=labels_for(logbook), ids=ids)


# -- the admitted read, carried while a door serves it ---------------------------------------

_admitted: ContextVar[g.Admission | None] = ContextVar("axiom_attest_admitted", default=None)


def current() -> g.Admission | None:
    return _admitted.get()


def clear() -> None:
    """Forget the admitted read once its door has served it."""
    _admitted.set(None)


def admitted_fields() -> dict[str, Any]:
    """What the read record of the door now serving adds: door, transport, labels."""
    from . import archives

    a = _admitted.get()
    if a is None:
        return {}  # nothing admitted: a refusal never names a package
    return {"door": a.door.id, "transport": a.transport, "labels": dict(a.target.labels or {}) or None,
            "reason": None, "refused": None, "status": 200, **archives.source_fields()}  # fmt: skip


def check(door_id: str, caller: Caller, the_target: Target, *, transport: str) -> Decision:
    """The one check for one of the log's doors; an allow is carried for the read record."""
    door = g.get(door_id)
    decision = g.check(door, caller, the_target)
    if decision.allowed:
        _admitted.set(g.Admission(door, caller, the_target, decision, transport))
    else:
        _admitted.set(None)
    return decision


def refused(door_id: str, reason: Reason, caller: Caller, the_target: Target) -> Decision:
    """A refusal decided before the check could run (the site step of a door)."""
    _admitted.set(None)
    return Decision(False, reason, "", None if reason is Reason.SITE else the_target.labels)


def record(
    door_id: str, caller: Caller, the_target: Target, decision: Decision, *, transport: str,
    ids: Any = (), count: int | None = None, params: dict[str, Any] | None = None,
    state_dir: Path | None = None, wire: tuple[int, str | None] | None = None,
    standalone: bool = False,
) -> None:  # fmt: skip
    """One read record for one answer at a door. Never raises (reads.py's isolation rule).

    ``standalone``: a skill door's read reached outside a skill invocation (memory recall
    re-resolving a reference), so nothing else would write the record: it is written here."""
    from . import reads

    door = g.get(door_id)
    try:
        fields = g.access_fields(door=door, decision=decision, caller=caller, target=the_target,
                                 transport=transport, ids=ids or (), count=count)  # fmt: skip
        if wire is not None and not decision.allowed:
            # The answer actually sent (a log the person can know of answers ``no_access``).
            fields["status"], fields["refused"] = wire
        if decision.allowed:
            from . import archives

            fields.update(archives.source_fields())  # a read from an archived period's package
        if transport == g.HTTP or door.shape == "listing" or standalone:
            audit_trail.record_invocation(
                state_dir=Path(state_dir or _state_dir()),
                skill=reads.DOOR_PREFIX + door.serves,
                params=params if params is not None else {"logbook": the_target.scope, "site": the_target.site},
                principal=caller.handle or audit_trail.DEFAULT_PRINCIPAL,
                site=the_target.site,
                outcome="ok" if decision.allowed else "denied",
                errors_count=0 if decision.allowed else 1,
                access=fields,
            )  # fmt: skip
        else:
            audit_trail.note_access(fields)
    except Exception as exc:  # noqa: BLE001 - a read never fails because its record did not
        reads.count_failure()
        log.warning("attest: read record for %s not written: %s", door_id, exc)


# -- skills: every read door is admitted here, once ------------------------------------------


def _skill_target(door: Door, params: dict[str, Any], site: str | None) -> tuple[Target, str | None]:
    """The target a skill asks for, and the record it names (a record id resolves its log)."""
    from . import service

    rid = params.get(door.record_param) if door.record_param else None
    if rid:
        rec = service.record(str(rid))
        if rec is None:
            return Target(site=site, scope=params.get("logbook"), labels=None, ids=(str(rid),)), "missing"
        return target(rec.get("site_id"), rec.get("logbook"), (str(rid),)), None
    return target(site, params.get("logbook") or None), None


def admit_skill(name: str, params: dict[str, Any], ctx: Any):
    """``(params, None)`` with the admitted site in them, or ``(None, refusal)``."""
    from . import archives
    from .skills._common import resolve_site

    archives.forget_source()  # a new read: no package has been read for it yet
    door = g.get(name)
    transport = _TRANSPORT.get(str(getattr(ctx, "surface", None) or "cli"), "cli")
    caller = caller_from_ctx(ctx)
    site = None
    if not (door.record_param and params.get(door.record_param)):
        site, err = resolve_site(params)
        if err and not params.get("site"):
            return None, _fail(err)  # a usage error (name the site); nothing was asked for
        if err:
            asked = Target(site=params.get("site"), scope=params.get("logbook"))
            reason = Reason.UNRESOLVED_CALLER if not caller.resolved else Reason.SITE
            decision = refused(name, reason, caller, asked)
            record(name, caller, asked, decision, transport=transport)
            return None, _refusal(decision)
    the_target, missing = _skill_target(door, params, site)
    if missing:
        decision = refused(name, Reason.NOT_PERMITTED if caller.resolved else Reason.UNRESOLVED_CALLER,
                           caller, the_target)  # fmt: skip
    elif door.scope_param and not params.get(door.scope_param) and door.id in _LISTS_ALL_LOGS:
        # A skill asked over every log the person may read: each log is admitted inside.
        decision = g.check(door, caller, Target(site=site, scope=None, labels=_UNSCOPED))
        if decision.allowed:
            _admitted.set(g.Admission(door, caller, Target(site=site), decision, transport))
    else:
        decision = check(name, caller, the_target, transport=transport)
    if not decision.allowed:
        record(name, caller, the_target, decision, transport=transport)
        return None, _refusal(decision)
    out = dict(params)
    if the_target.site:
        out["site"] = the_target.site
    return out, None


#: A skill context's surface, as the door's transport: chat reaches skills as agent tools.
_TRANSPORT = {"cli": "cli", "mcp": "mcp", "agent_tool": "agent_tool", "chat": "agent_tool"}

#: Skills that, with no log named, read every log the person may read (each admitted inside).
_LISTS_ALL_LOGS = frozenset({"attest.obligations", "attest.access"})
#: The labels of a request over no one log: the platform default, so only site, GUARD and the
#: caller are decided; each log read inside is admitted with its own labels.
_UNSCOPED = {"classification": "public", "export_controlled": False}


def _fail(message: str):
    from .skills._common import fail

    return fail(message)


def _refusal(decision: Decision):
    from axiom.infra.skills import SkillResult

    return SkillResult(ok=False, value=decision.body(), errors=[g.DETAIL[decision.reason]])  # type: ignore[index]


def refuse_at_dispatch(name: str, params: dict[str, Any], ctx: Any):
    """A pre-check refused a read skill before its door ran: the door's own answer and record.
    The one check decides the reason; when it would have allowed, the pre-check was stricter
    (a site or tool rule) and the answer is ``not_permitted``. Never an allow."""
    admitted, refusal = admit_skill(name, params, ctx)
    if refusal is not None:
        return refusal
    a = _admitted.get()
    _admitted.set(None)
    caller = a.caller if a is not None else caller_from_ctx(ctx)
    the_target = a.target if a is not None else Target(site=params.get("site"), scope=params.get("logbook"))
    decision = Decision(False, Reason.NOT_PERMITTED, "a pre-check refused", the_target.labels)
    record(name, caller, the_target, decision, transport=a.transport if a is not None else "cli")
    return _refusal(decision)


g.set_refuser(EXT, refuse_at_dispatch)


def readable(caller: Caller, door_id: str, site: str | None, logbook: str) -> bool:
    """Whether one log is readable through a listing door (no record of its own)."""
    return g.check(g.get(door_id), caller, target(site, logbook)).allowed


def admitted_skill(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a read skill so the one check runs before it and its read is always recorded."""

    @functools.wraps(fn)
    def run(params, ctx=None):
        admitted, refusal = admit_skill(name, dict(params or {}), ctx)
        if refusal is not None:
            return refusal
        token = audit_trail.take_access()  # nothing stale rides into this read's record
        del token
        result = fn(admitted, ctx)
        a = _admitted.get()
        if getattr(result, "ok", False) and a is not None:
            noted = audit_trail.take_access()
            fields = g.access_fields(door=a.door, decision=a.decision, caller=a.caller,
                                     target=a.target, transport=a.transport,
                                     ids=(noted or {}).get("ids") or a.target.ids,
                                     count=(noted or {}).get("count"))  # fmt: skip
            if noted:
                fields["surface"] = noted.get("surface") or fields["surface"]
                fields["what"] = noted.get("what") or fields["what"]
                fields["logbook"] = noted.get("logbook") or fields["logbook"]
            from . import archives

            fields.update(archives.source_fields())  # a read from an archived period's package
            audit_trail.note_access(fields)
        _admitted.set(None)
        return result

    return run


__all__ = [
    "DOORS",
    "LogCaller",
    "NOT_DOORS",
    "admit_skill",
    "admitted_fields",
    "admitted_skill",
    "bind_request_caller",
    "request_caller",
    "unbind_request_caller",
    "caller_from_ctx",
    "caller_from_session",
    "check",
    "clear",
    "current",
    "labels_for",
    "readable",
    "record",
    "refused",
    "rule",
    "target",
]
