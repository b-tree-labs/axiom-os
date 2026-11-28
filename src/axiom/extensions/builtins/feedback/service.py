# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Feedback from people and agents, grouped by cause, rolled back by default (LOOP-B1 to B7).

One report is one action: good or bad, an optional sentence (B1). The server fills in the
rest (B2): the surface, the release, the node, the environment, every flag that resolved on
for the reporter and what decided it, and the capability they last called here
(:mod:`axiom.infra.capability_outcomes`) or the screen the web app names. An agent reports a
call that failed, was refused or returned something it could not use the same way, and the
call's outcome and first error are attached (B3); nothing is reported unless it says so.

**The implicated flag.** A flag the reporter names is taken only when it resolved on for them,
so nobody can implicate (or stop) a flag they were never exposed to. Otherwise the flags the
reporter was *exposed* to (on by a rollout or an override, not by default or the
environment) are narrowed to the ones the capability's or screen's extension declares; one
left is implicated. None left, or several: the release is the cause.

**Grouping (B4).** Reports with one release, one implicated flag and one capability or screen
are one group, counted, with distinct reporters.

**The ask (B5, B7: owner decisions 2026-11-12).** The first bad report opens the group as one
ask for its owner: the implicated flag's owner (LOOP-F11, ``owned_by``; until a flag has one,
the site admins), or for a release the ``feedback.release_owner`` setting (default the site
admins). Rollback is already chosen: "Stop this flag" when a flag is implicated, "Roll back the
slot" when the release is; "Fix forward instead" needs a reason. Needs you lists it for the owner
only (``queue.for_person``); one notification goes through HERALD, which follows the person's own
settings (ADR-238).

**The automatic stop.** With ``feedback_auto_stop`` on, a bad report marked severe, or the same
cause reported bad by ``feedback.auto_stop.reporters`` distinct reporters within
``feedback.auto_stop.window_minutes`` (defaults 3 and 60), stops the implicated flag through the
existing kill path (``flags.kill``, as RIVET, through the dispatch chokepoint, so it is audited
like any stop) with the reason ``auto: N reports of <cause>``; the public message is the standard
line. The owner gets one notification. A slot is never rolled back from feedback: it is offered.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from . import store

log = logging.getLogger(__name__)

GOOD, BAD = "good", "bad"
VERDICTS = (GOOD, BAD)
OUTCOMES = ("failed", "refused", "unusable")
STOP_FLAG, ROLL_BACK_SLOT, FIX_FORWARD = "stop_flag", "roll_back_slot", "fix_forward"
CHOICES = (STOP_FLAG, ROLL_BACK_SLOT, FIX_FORWARD)
LABELS = {STOP_FLAG: "Stop this flag", ROLL_BACK_SLOT: "Roll back the slot", FIX_FORWARD: "Fix forward instead"}
STATUS_AFTER = {STOP_FLAG: "stopped", ROLL_BACK_SLOT: "rolling_back", FIX_FORWARD: "fixing_forward"}

#: The flag this phase ships behind, and the one the automatic stop ships behind.
REPORTS_FLAG = "feedback_reports"
AUTO_STOP_FLAG = "feedback_auto_stop"
#: Who acts for the platform when it stops a flag by itself (the loop's automatic actions).
ACTOR = "@rivet"
#: The role that answers when no owner is named (the site's site admins).
SITE_ADMINS = "@site-admin"
#: Flag sources that mean the subject was exposed on purpose, not by default (LOOP-F7).
EXPOSURE = ("rollout", "user", "account", "node")
MAX_TEXT = 1000

DEFAULTS: dict[str, Any] = {
    "feedback.auto_stop.reporters": 3,
    "feedback.auto_stop.window_minutes": 60,
    "feedback.release_owner": SITE_ADMINS,
}

_settings: Mapping[str, Any] | None = None


def set_settings(values: Mapping[str, Any] | None) -> None:
    """Pin the site settings this module reads (tests); ``None`` reads the settings store."""
    global _settings
    _settings = values


def setting(key: str) -> Any:
    if _settings is not None:
        return _settings.get(key, DEFAULTS.get(key))
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        value = SettingsStore().get(key, None)
    except Exception:  # noqa: BLE001 - an unreadable settings file means the documented default
        value = None
    return DEFAULTS.get(key) if value in (None, "") else value


class Refused(Exception):
    pass


# ------------------------------------------------------------------ context (B2, B3)


def _flags():
    from axiom.extensions.builtins.features.skills._flags import _service

    return _service()


def this_node() -> str:
    from axiom.extensions.builtins.features.skills._flags import this_node as _node

    return _node()


def is_on(key: str) -> bool:
    try:
        return _flags().resolve_flag(key, node=this_node()) == "on"
    except Exception:  # noqa: BLE001 - an unreadable flag store keeps a new behaviour off
        return False


def release() -> str:
    from axiom.infra.revision import installed_version

    return installed_version() or "unknown"


def environment() -> str:
    from axiom.extensions.builtins.features.flag_registry import get_environment

    return get_environment()


def user_of(handle: str) -> str:
    """``@ana:local`` -> ``ana``: the user id a flag override is stored under."""
    return handle.lstrip("@").split(":", 1)[0]


def flags_on(subject: Mapping[str, Any]) -> dict[str, str]:
    """``{key: source}`` for every flag that resolved to something other than off or soon."""
    try:
        svc = _flags()
        res = svc.resolve(node=this_node(), **{k: v for k, v in subject.items() if v not in (None, "")})
    except Exception as exc:  # noqa: BLE001 - a report is never lost because flags could not be read
        log.warning("feedback: flags could not be resolved for the report: %s", exc)
        return {}
    return {k: res.sources.get(k, "") for k, v in res.flags.items() if v not in ("off", "soon")}


def _namespace(where: str | None) -> str:
    if not where:
        return ""
    text = where.strip().lstrip("#/")
    for sep in (".", "/", "?"):
        text = text.split(sep, 1)[0]
    return text


def implicated(named: str | None, on: Mapping[str, str], where: str | None) -> str | None:
    """The one flag this report points at, or ``None`` when the release is the cause."""
    if named:
        if named not in on:
            raise Refused(f"{named} is not on for you, so a report cannot implicate it; "
                          "leave the flag out and the server finds it")
        return named
    exposed = [k for k, src in on.items() if src in EXPOSURE]
    ns = _namespace(where)
    if ns:
        try:
            registry = _flags().get_registry()
        except Exception:  # noqa: BLE001
            registry = {}
        narrowed = [k for k in exposed if getattr(registry.get(k), "owner", "") == ns]
        if len(narrowed) == 1:
            return narrowed[0]
        if narrowed:
            return None
    return exposed[0] if len(exposed) == 1 else None


def owner_of(flag: str | None) -> str:
    if flag:
        try:
            owned = str(getattr(_flags().get_registry().get(flag), "owned_by", "") or "")
        except Exception:  # noqa: BLE001
            owned = ""
        return owned or SITE_ADMINS
    return str(setting("feedback.release_owner") or SITE_ADMINS)


def cause_label(group: Mapping[str, Any]) -> str:
    what = f"the flag {group['flag']}" if group.get("flag") else f"release {group.get('release')}"
    where = group.get("capability") or group.get("screen")
    return f"{what} at {where}" if where else what


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ------------------------------------------------------------------ report (B1 to B5)


def report(params: Mapping[str, Any], *, reporter: str, surface: str | None,
           subject: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Store one report with its captured context, group it, and act on its group."""
    from axiom.infra import capability_outcomes

    verdict = str(params.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise Refused("say good or bad")
    if not reporter:
        raise Refused("a report needs a signed-in reporter")
    text = str(params.get("text") or "").strip()[:MAX_TEXT] or None
    outcome = str(params.get("outcome") or "").strip().lower() or None
    if outcome and outcome not in OUTCOMES:
        raise Refused(f"outcome is one of {', '.join(OUTCOMES)}")
    capability = str(params.get("capability") or "").strip() or None
    # The web app names the screen; on any other surface the call is the context.
    screen = (str(params.get("screen") or "").strip() or None) if surface == "web" else None
    severe = verdict == BAD and str(params.get("severe") or "").strip().lower() in ("1", "true", "yes", "on")

    # B3: the call the agent flags, from the dispatch hook; B2: else the reporter's last call here.
    error = None
    call = capability_outcomes.last(reporter, capability) if capability else (
        None if screen else capability_outcomes.last(reporter, skip_prefix="feedback."))
    if call is not None:
        capability = call.capability
        if call.outcome != capability_outcomes.OK:
            outcome = outcome or call.outcome
            error = call.error or None
    if outcome and verdict != BAD:
        raise Refused("a failed, refused or unusable call is a bad report")

    who = dict(subject or {}) or {"user": user_of(reporter)}
    on = flags_on(who)
    flag = implicated(str(params.get("flag") or "").strip() or None, on, capability or screen)
    rel, env = release(), environment()
    group = {
        "cause_key": "|".join((rel, flag or "", capability or "", "" if capability else (screen or ""))),
        "release": rel, "flag": flag, "environment": env, "capability": capability,
        "screen": None if capability else screen, "owner": owner_of(flag),
        "rollback": STOP_FLAG if flag else ROLL_BACK_SLOT,
    }
    kind = "person" if surface == "web" else ("agent" if outcome else (who.get("principal_kind") or None))
    row, grp, _new = store.add({
        "reporter": reporter, "reporter_kind": kind, "verdict": verdict, "severe": severe, "text": text,
        "surface": surface, "release": rel, "node": this_node(), "environment": env,
        "capability": capability, "screen": screen, "outcome": outcome, "error": error,
        "flags_on": on, "flag": flag,
    }, group)
    grp = store.get_group(grp["id"]) or grp

    acted: dict[str, Any] | None = None
    if verdict == BAD and grp["status"] == "open":
        acted = _maybe_stop(grp, severe=severe)
        notices = {n.get("kind") for n in grp["notices"]}
        if acted is None and "opened" not in notices:
            _notify(grp, "opened")
    return {"report": row, "group": store.get_group(grp["id"]), "auto_stop": acted, "new_cause": _new}


def _maybe_stop(grp: dict[str, Any], *, severe: bool) -> dict[str, Any] | None:
    """Stop the implicated flag by itself when the owner's rule says so (B5, B7)."""
    if not grp.get("flag") or not is_on(AUTO_STOP_FLAG):
        return None
    window = timedelta(minutes=float(setting("feedback.auto_stop.window_minutes")))
    need = int(setting("feedback.auto_stop.reporters"))
    seen = store.distinct_reporters(grp["id"], since=datetime.now(UTC) - window)
    if not severe and seen < need:
        return None
    reason = f"auto: {_count(grp['bad'], 'report')} of {cause_label(grp)}" + (" (marked severe)" if severe else "")
    result = _kill(grp, reason=reason, actor=ACTOR)
    if result.ok:
        store.update_group(grp["id"], status="stopped", decided_by=ACTOR, decided_at=datetime.now(UTC),
                           decision_reason=reason)
        _resolve_notices(grp, ("opened",))
    _notify(store.get_group(grp["id"]) or grp, "stopped" if result.ok else "stop_failed",
            errors=list(result.errors))
    return {"stopped": result.ok, "reason": reason, "by": ACTOR, "errors": list(result.errors),
            "reporters": seen, "severe": severe}


def _kill(grp: Mapping[str, Any], *, reason: str, actor: str):
    """Through the existing kill path and the dispatch chokepoint: audited like any stop."""
    from axiom.extensions.builtins.features.skills import bind
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import RUNNER_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    ctx = SkillContext(registry=reg, state_dir=get_user_state_dir(), logger=log, actor=actor)
    params = {"key": grp["flag"], "reason": reason, "engaged": True}
    if grp.get("environment"):
        params["environment"] = grp["environment"]
    return invoke_capability(reg, "flags.kill", params, ctx, surface=RUNNER_SURFACE)


# ------------------------------------------------------------------ the owner's notification


def href(grp: Mapping[str, Any]) -> str:
    return f"#/settings/flags/{grp['flag']}?view=people&group={grp['id']}" if grp.get("flag") \
        else f"#/feedback?group={grp['id']}"


def _notify(grp: Mapping[str, Any], kind: str, *, errors: list[str] | None = None) -> None:
    """One notification per ask, and one per automatic stop (ADR-238 D4). Never raises."""
    cause = cause_label(grp)
    people = grp.get("reporters") or 0
    counted = f"{_count(grp['bad'], 'bad report')} from {_count(people, 'reporter')}"
    if kind == "opened":
        summary, priority = f"{counted} about {cause}", "high" if grp.get("severe") else "normal"
        body = f"{summary}.\n\nChosen: {LABELS[grp['rollback']]}. Fix forward instead needs a reason."
    elif kind == "stopped":
        summary, priority = f"RIVET stopped {grp['flag']}: {counted}", "high"
        body = (f"{summary} about {cause}.\n\nRIVET did: stopped the flag ({grp.get('decision_reason')}). "
                f"You decide: fix it, then release the stop.")
    else:
        summary, priority = f"Could not stop {grp['flag']} after {counted}", "high"
        body = f"{summary}: {'; '.join(errors or []) or 'the stop failed'}.\n\nChosen: Stop this flag."
    receipt = None
    try:
        from axiom.extensions.builtins.notifications.send import NotificationPayload, Priority, send
        from axiom.extensions.builtins.notifications.skills.send import _ctx
        from axiom.governance import Classification

        receipt = send(
            _ctx(), actor=ACTOR, recipient=grp["owner"],
            payload=NotificationPayload(summary=summary, body=f"{body}\n\n{href(grp)}",
                                        metadata={"kind": f"feedback.{kind}", "group": grp["id"]}),
            classification=Classification.INTERNAL, priority=Priority(priority),
            intent=f"feedback.{kind}", dedup_key=f"feedback:{grp['id']}:{kind}",
        )
    except Exception as exc:  # noqa: BLE001 - a failed send never loses the report or the stop
        log.warning("feedback: the %s notice for %s was not sent: %s", kind, grp.get("id"), exc)
    store.add_notice(grp["id"], {"kind": kind, "receipt": getattr(receipt, "id", None),
                                 "at": datetime.now(UTC).isoformat()})


def _resolve_notices(grp: Mapping[str, Any], kinds: tuple[str, ...]) -> None:
    """Acting on the ask resolves its notification everywhere (ADR-253 D5). Never raises."""
    receipts = {n.get("receipt") for n in grp.get("notices") or [] if n.get("kind") in kinds and n.get("receipt")}
    if not receipts:
        return
    try:
        from axiom.extensions.builtins.notifications.inbox import InboxQuery, mark_read
        from axiom.extensions.builtins.notifications.preferences import resolve_role
        from axiom.extensions.builtins.notifications.skills.send import _ctx

        inbox = _ctx().inbox_store
        for row in inbox.query(InboxQuery(recipient=resolve_role(grp["owner"]), unread_only=True, limit=500)):
            if row.receipt_id in receipts:
                mark_read(inbox, row_id=row.id)
    except Exception as exc:  # noqa: BLE001 - a notice left unread never undoes the decision
        log.warning("feedback: the notices of %s were not marked read: %s", grp.get("id"), exc)


# ------------------------------------------------------------------ the owner decides (B5)


def holds(caller: str, owner: str, roles: tuple[str, ...] = ()) -> bool:
    """The caller is the owner, holds the owner role on this node, or (on the web) by session role."""
    if not caller or not owner:
        return False
    if caller == owner or owner.lstrip("@").split(":", 1)[0] in roles:
        return True
    try:
        from axiom.extensions.builtins.notifications.preferences import resolve_role

        return resolve_role(owner) == caller
    except Exception:  # noqa: BLE001
        return False


def decide(group_id: str, choice: str, *, caller: str, reason: str = "",
           roles: tuple[str, ...] = ()) -> dict[str, Any]:
    grp = store.get_group(group_id)
    if grp is None:
        raise Refused(f"no feedback group {group_id!r}")
    if not holds(caller, grp["owner"], roles):
        raise Refused(f"only {grp['owner']}, who owns this, decides it")
    if choice not in CHOICES:
        raise Refused(f"choose one of {', '.join(CHOICES)}")
    if grp["status"] != "open":
        raise Refused(f"already decided: {grp['status']} by {grp['decided_by']}")
    reason = reason.strip()
    if choice == FIX_FORWARD and not reason:
        raise Refused("fixing forward instead of rolling back needs a reason")
    if choice == STOP_FLAG and not grp.get("flag"):
        raise Refused("no flag is implicated; roll back the slot or fix forward")
    out: dict[str, Any] = {}
    if choice == STOP_FLAG:
        why = reason or f"owner: {_count(grp['bad'], 'report')} of {cause_label(grp)}"
        result = _kill(grp, reason=why, actor=caller)
        if not result.ok:
            raise Refused("; ".join(result.errors) or "the stop did not apply")
        reason = why
    elif choice == ROLL_BACK_SLOT:
        # ADR-197: rollback sends traffic back to the slot still running the previous release.
        out["next"] = ("send traffic back to the previous slot: "
                       "python -m axiom.infra.deploy.bluegreen <compose|k8s> --role <role> --to <previous slot>")
    updated = store.update_group(group_id, status=STATUS_AFTER[choice], decided_by=caller,
                                 decided_at=datetime.now(UTC), decision_reason=reason or None)
    _resolve_notices(grp, ("opened", "stop_failed"))
    return {"group": updated, "choice": choice, **out}


# ------------------------------------------------------------------ a flag's people view (B6)


def for_flag(key: str, *, limit: int = 20) -> dict[str, Any]:
    groups = store.groups(flag=key, limit=limit)
    for g in groups:
        g["recent"] = [{k: r[k] for k in ("created_at", "verdict", "severe", "text", "reporter",
                                          "reporter_kind", "surface", "outcome")}
                       for r in store.reports(g["id"], limit=5)]
    return {"flag": key, "groups": groups,
            "bad": sum(g["bad"] for g in groups), "good": sum(g["good"] for g in groups)}


__all__ = [
    "AUTO_STOP_FLAG", "CHOICES", "DEFAULTS", "LABELS", "REPORTS_FLAG", "Refused", "cause_label", "decide",
    "for_flag", "holds", "href", "implicated", "is_on", "report", "set_settings", "setting",
]
