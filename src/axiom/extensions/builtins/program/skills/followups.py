# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.followup_*`` - durable reminders (ADR-196).

A follow-up is a tracked commitment to come back to something: the owner, the
due time, an optional deterministic check, and the evidence it was closed. CLERK
owns the record and its journal; the schedule substrate keeps the clock; HERALD
delivers (the clock and delivery are ``followup_fire`` / ``followup_sweep``).

These skills are commons (ADR-161): any principal in any harness reaches them
through the composed MCP and the CLI. The calling principal is the transport
identity (over MCP, the ADR-114 gate's caller, because the skill context's own
principal is the node owner on that path), never a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import followup_clock as clock
from .. import followup_model as fm
from .. import followup_store as store
from ._mutate import FORBIDDEN, program_maintainers, refuse
from ._source import ABSENT, BAD_REQUEST, default_data_path

_log = logging.getLogger("axiom.program.followup")

#: Test seam: tests replace this to drive time deterministically.
_now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731


def caller(ctx: SkillContext) -> str:
    """The acting principal: the MCP gate's caller on MCP, else the context's."""
    if ctx.surface == "mcp":
        from axiom.extensions.builtins.mcp.identity_gate import caller_principal

        return caller_principal()
    return ctx.principal.handle


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def _as_list(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return list(raw)


def _as_dict(raw: Any) -> dict[str, Any] | None:
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            raise fm.FollowupError("check_args must be a JSON object.", fix="""e.g. --check-args '{"table": "orders_old"}'""") from None
    if not isinstance(raw, dict):
        raise fm.FollowupError("check_args must be an object of argument names to values.")
    return raw


def _public(row: dict[str, Any], *, now: datetime, delivered: bool | None = None) -> dict[str, Any]:
    if delivered is None:
        delivered = any(
            e["kind"] == "delivered" for e in store.events(row["id"], kind="delivered", cycle=row["cycle"])
        )
    out = {
        "id": row["id"],
        "what": row["what"],
        "owner": row["owner"],
        "created_by": row["created_by"],
        "state": row["state"],
        "due_at": _iso(row["due_at"]),
        "check": row["check_skill"],
        "next_command": row["next_command"],
        "shared_with": row["shared_with"],
        "evidence": row["evidence"],
        "expires_at": _iso(row["expires_at"]),
        "snoozes": row["cycle"],
        "missed": fm.is_missed(row, now=now, delivered_this_cycle=delivered),
    }
    if row["acknowledged_at"]:
        out["acknowledged_by"], out["acknowledged_at"] = row["acknowledged_by"], _iso(row["acknowledged_at"])
    if row["closed_at"]:
        out["closed_by"], out["closed_at"], out["closed_note"] = row["closed_by"], _iso(row["closed_at"]), row["closed_note"]
    return out


def _bad(exc: fm.FollowupError) -> SkillResult:
    return refuse(exc.kind, str(exc))


def _visible(followup_id: Any, who: str) -> tuple[dict[str, Any] | None, SkillResult | None]:
    fid = str(followup_id or "").strip()
    if not fid:
        return None, refuse(BAD_REQUEST, "id is required; list yours with program.followup_list")
    row = store.get(fid)
    # An id the caller may not see is reported exactly as one that does not exist.
    if row is None or not fm.visible_to(row, who):
        return None, refuse(ABSENT, f"no follow-up {fid!r} visible to {who}; list yours with program.followup_list")
    return row, None


def _may_assign(who: str, ctx: SkillContext) -> bool:
    """Creating a follow-up owned by someone else is a deputy or maintainer action."""
    from ..model import ProgramError, load_program

    path = default_data_path(ctx)
    try:
        data = load_program(path)
    except (ProgramError, OSError):
        return False
    return who == data.deputy() or who in program_maintainers(data)


def _arm(row: dict[str, Any], *, now: datetime, actor: str) -> dict[str, Any]:
    """Point the PULSE clock at the follow-up's next action. Never loses the record."""
    at = row["next_action_at"]
    old = row.get("schedule_id")
    try:
        sid = clock.rearm(row["id"], old, at)
        store.update(row["id"], schedule_id=sid)
        store.add_event(row["id"], "armed", actor, now=now, cycle=row["cycle"], rung=row["rung"],
                        detail={"at": _iso(at), "schedule_id": sid})
        row["schedule_id"] = sid
    except Exception as exc:  # noqa: BLE001 - the record is durable; the sweep re-arms it
        _log.warning("could not arm follow-up %s: %s", row["id"], type(exc).__name__)
        store.add_event(row["id"], "arm_failed", actor, now=now, cycle=row["cycle"],
                        detail={"error": type(exc).__name__})
    return row


def _check_ok(spec_name: str | None, args: dict | None, ctx: SkillContext) -> None:
    if not spec_name:
        if args:
            raise fm.FollowupError("check_args given without a check.", fix="name the check skill, e.g. check='data.table_status'")
        return
    spec = ctx.registry.spec(spec_name)
    if spec is None:
        raise fm.FollowupError(
            f"check {spec_name!r} is not a registered skill.",
            fix="list skills with 'axi skills list'; the check must be a read-only skill",
        )
    if spec.effective_risk != "read" or spec.side_effects is not False:
        raise fm.FollowupError(
            f"check {spec_name!r} is not read-only (risk={spec.effective_risk}).",
            fix="a check runs unattended at due time, so it must declare risk='read' and side_effects=False",
        )
    declared = set((spec.inputs or {}))
    if declared:
        unknown = sorted(set(args or {}) - declared)
        if unknown:
            raise fm.FollowupError(
                f"check {spec_name!r} takes no argument {', '.join(unknown)}.",
                fix=f"it declares: {', '.join(sorted(declared))}",
            )
    fm.refuse_secretish_keys(args)


# ---------------------------------------------------------------------------


def add(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Create a follow-up: owner, due time, optional read-only check, and arm its clock."""
    now = _now()
    who = caller(ctx)
    try:
        what = fm.clean_text(params.get("what"), name="what", limit=fm.WHAT_MAX, required=True)
        due = fm.parse_due(str(params.get("due") or ""), now=now)
        owner = fm.need_principal(params.get("owner") or who, "owner")
        shared = [fm.need_principal(p, "shared_with entry") for p in _as_list(params.get("shared_with"))]
        check = fm.clean_text(params.get("check"), name="check", limit=200)
        args = _as_dict(params.get("check_args"))
        command = fm.clean_text(params.get("next_command"), name="next_command", limit=fm.COMMAND_MAX)
        evidence = _evidence(_as_list(params.get("evidence")))
        ladder = fm.validate_ladder(_as_list(params.get("ladder")) or None)
        fm.refuse_secrets({"what": what, "next_command": command, "check_args": args,
                           "evidence": evidence, "check": check})
        _check_ok(check, args, ctx)
    except fm.FollowupError as exc:
        return _bad(exc)
    if owner != who and not _may_assign(who, ctx):
        return refuse(
            FORBIDDEN,
            f"{who} may not create a follow-up owned by {owner}. Fix: add it for yourself, "
            "or ask the program deputy or a maintainer to assign it",
        )
    record = {
        "id": store.new_id(), "what": what, "owner": owner, "created_by": who, "created_at": now,
        "original_due_at": due, "due_at": due, "state": "open", "check_skill": check, "check_args": args,
        "next_command": command, "ladder": ladder, "cycle": 0, "rung": 0,
        "next_action_at": fm.rung_time(due, ladder, 0), "schedule_id": None, "shared_with": sorted(set(shared) - {owner, who}),
        "evidence": evidence, "expires_at": due + fm.EXPIRY_AFTER_DUE,
    }
    row = store.create(record, actor=who, now=now)
    row = _arm(row, now=now, actor=who)
    try:
        clock.ensure_sweep(now=now)
    except Exception as exc:  # noqa: BLE001 - the orchestrator also ensures it at start-up
        _log.info("could not ensure the follow-up heartbeat now (%s)", type(exc).__name__)
    view = _public(row, now=now, delivered=False)
    view["armed"] = bool(row.get("schedule_id"))
    note = "" if view["armed"] else " (the clock could not be armed now; the sweep will arm it)"
    return SkillResult(
        ok=True, value=view, actions_taken=[f"created follow-up {row['id']} for {owner} due {_iso(due)}{note}"],
    )


def _evidence(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for e in raw or []:
        if isinstance(e, str):
            e = {"label": e, "url": e if "://" in e else ""}
        if not isinstance(e, dict) or not (e.get("label") or e.get("url")):
            raise fm.FollowupError("each evidence entry needs a label or a url.")
        out.append({"label": str(e.get("label") or e.get("url"))[:200], "url": str(e.get("url") or "")[:500]})
    return out


def list_(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The calling principal's follow-ups (owned, created, or shared with them)."""
    now = _now()
    who = caller(ctx)
    role = str(params.get("role") or "any")
    if role not in ("any", "owner", "created", "shared"):
        return refuse(BAD_REQUEST, "role must be one of any, owner, created, shared")
    wanted = [s.strip() for s in str(params.get("state") or "").split(",") if s.strip()]
    bad = [s for s in wanted if s not in fm.STATES]
    if bad:
        return refuse(BAD_REQUEST, f"unknown state {', '.join(bad)}; states are {', '.join(fm.STATES)}")
    include_done = bool(params.get("include_done")) or "done" in wanted
    try:
        limit = max(1, min(int(params.get("limit") or 50), 200))
    except (TypeError, ValueError):
        return refuse(BAD_REQUEST, "limit must be a number")
    rows = []
    for r in store.all_rows():
        if not fm.visible_to(r, who):
            continue
        if role == "owner" and r["owner"] != who:
            continue
        if role == "created" and r["created_by"] != who:
            continue
        if role == "shared" and who not in r["shared_with"]:
            continue
        if wanted and r["state"] not in wanted:
            continue
        if r["state"] == "done" and not include_done:
            continue
        rows.append(r)
    items = [_public(r, now=now) for r in rows[:limit]]
    return SkillResult(ok=True, value={"principal": who, "count": len(items), "followups": items})


def owed(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """What does the calling principal owe? Missed and due first; quiet when nothing is."""
    now = _now()
    who = caller(ctx)
    return SkillResult(ok=True, value=owed_for(who, now=now))


def owed_for(who: str, *, now: datetime, compact: bool = False) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in ("missed", "due", "expired", "open", "snoozed", "acknowledged")}
    for r in store.all_rows(states=fm.OWED):
        if r["owner"] != who and who not in r["shared_with"]:
            continue
        v = _public(r, now=now)
        missed = v["missed"]
        if compact:
            v = {k: v[k] for k in ("id", "what", "due_at", "state", "owner", "next_command") if v.get(k) is not None}
        groups["missed" if missed else r["state"]].append(v)
    total = sum(len(v) for v in groups.values())
    out: dict[str, Any] = {"principal": who, "total": total, **groups}
    out["summary"] = (
        "nothing owed" if total == 0 else
        ", ".join(f"{len(v)} {k}" for k, v in groups.items() if v)
    )
    return out


def show(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    now = _now()
    row, refusal = _visible(params.get("id"), caller(ctx))
    if refusal:
        return refusal
    assert row is not None
    view = _public(row, now=now)
    view["journal"] = [
        {"at": _iso(e["at"]), "kind": e["kind"], "actor": e["actor"], "cycle": e["cycle"], "rung": e["rung"],
         "detail": e["detail"]}
        for e in store.events(row["id"])
    ]
    view["commands"] = fm.next_command_hint(row)
    return SkillResult(ok=True, value=view)


def snooze(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Move the due time. Starts a new ladder run from the new time."""
    now = _now()
    who = caller(ctx)
    row, refusal = _visible(params.get("id"), who)
    if refusal:
        return refusal
    assert row is not None
    if row["state"] == "done":
        return refuse(BAD_REQUEST, f"{row['id']} is already done")
    when = params.get("until") or params.get("by")
    try:
        due = fm.parse_due(str(when or ""), now=now)
    except fm.FollowupError as exc:
        return _bad(exc)
    cycle = row["cycle"] + 1
    ladder = row["ladder"]
    row = store.update(
        row["id"], due_at=due, state="snoozed", cycle=cycle, rung=0, next_action_at=fm.rung_time(due, ladder, 0),
        expires_at=due + fm.EXPIRY_AFTER_DUE, acknowledged_by=None, acknowledged_at=None,
    ) or row
    store.add_event(row["id"], "snooze", who, now=now, cycle=cycle, detail={"to": _iso(due)})
    row = _arm(row, now=now, actor=who)
    return SkillResult(ok=True, value=_public(row, now=now, delivered=False),
                       actions_taken=[f"snoozed {row['id']} to {_iso(due)}"])


def ack(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Say you have seen it: stops the ladder. It stays owed until done."""
    now = _now()
    who = caller(ctx)
    row, refusal = _visible(params.get("id"), who)
    if refusal:
        return refusal
    assert row is not None
    if row["state"] == "acknowledged":
        return SkillResult(ok=True, value=_public(row, now=now), actions_taken=["already acknowledged"])
    if row["state"] not in ("due", "expired"):
        return refuse(
            BAD_REQUEST,
            f"{row['id']} is {row['state']}, not yet due, so there is nothing to acknowledge. "
            "Fix: snooze it to move the time, or mark it done",
        )
    try:
        note = fm.clean_text(params.get("note"), name="note", limit=500)
        fm.refuse_secrets({"note": note})
    except fm.FollowupError as exc:
        return _bad(exc)
    row = store.update(row["id"], state="acknowledged", acknowledged_by=who, acknowledged_at=now, next_action_at=None) or row
    clock.disarm(row.get("schedule_id"))
    store.update(row["id"], schedule_id=None)
    store.add_event(row["id"], "ack", who, now=now, cycle=row["cycle"], detail={"note": note} if note else {})
    row = store.get(row["id"]) or row
    return SkillResult(ok=True, value=_public(row, now=now), actions_taken=[f"acknowledged {row['id']}"])


def done(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Close it, with a note and optional evidence."""
    now = _now()
    who = caller(ctx)
    row, refusal = _visible(params.get("id"), who)
    if refusal:
        return refusal
    assert row is not None
    if row["state"] == "done":
        return SkillResult(ok=True, value=_public(row, now=now), actions_taken=["already done"])
    try:
        note = fm.clean_text(params.get("note"), name="note", limit=1000)
        extra = _evidence(_as_list(params.get("evidence")))
        fm.refuse_secrets({"note": note, "evidence": extra})
    except fm.FollowupError as exc:
        return _bad(exc)
    clock.disarm(row.get("schedule_id"))
    row = store.update(
        row["id"], state="done", closed_by=who, closed_at=now, closed_note=note, next_action_at=None,
        schedule_id=None, evidence=row["evidence"] + extra,
    ) or row
    store.add_event(row["id"], "done", who, now=now, cycle=row["cycle"], detail={"note": note} if note else {})
    return SkillResult(ok=True, value=_public(row, now=now), actions_taken=[f"closed {row['id']}"])


__all__ = ["ack", "add", "caller", "done", "list_", "owed", "owed_for", "show", "snooze"]
