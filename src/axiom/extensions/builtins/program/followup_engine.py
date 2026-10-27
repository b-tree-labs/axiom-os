# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fire and the sweep (ADR-196 D2, D3).

``fire`` is what PULSE runs at a follow-up's next action time. It is idempotent
by construction, because PULSE re-runs a fire whose process was killed:

1. ``attempt`` is journaled first (a unique key per cycle and rung);
2. the check runs, the reminder is composed from the check's allowlisted output;
3. it is sent through HERALD under the dedup key ``followup:<id>:<cycle>:<rung>``,
   so a re-run after a crash between send and record is suppressed by HERALD;
4. ``delivered`` is journaled (a unique key) and only then does the follow-up
   advance and the next one-shot get registered.

A re-run that finds ``delivered`` skips straight to the advance. The effect is
exactly one delivery per rung.

``sweep`` is the heartbeat: it re-arms any open follow-up that lost its clock
row, expires follow-ups past their horizon, and marks follow-ups that were due
and never delivered as missed. It writes nothing when there is nothing to do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import followup_check as check_mod
from . import followup_clock as clock
from . import followup_model as fm
from . import followup_store as store

_log = logging.getLogger("axiom.program.followup")

ACTOR = "@clerk:local"
#: A fire this much before its recorded time is treated as early, not due.
_TOLERANCE = timedelta(seconds=90)
OPERATOR_ENV = "AXI_FOLLOWUP_OPERATOR"


@dataclass(frozen=True)
class Sent:
    """What HERALD reported for one send."""

    ok: bool
    channel: str | None
    durable: bool
    suppressed: bool = False
    receipt_id: str | None = None
    error: str | None = None

    @property
    def reach(self) -> str:
        if not self.ok or not self.durable:
            return "unreachable"
        return "inbox-only" if self.channel in (None, "inbox") else "pushed"


Sender = Callable[[str, str, str, str, str], Sent]
#: (recipient, subject, body, dedup_key, priority) -> Sent


def herald_send(recipient: str, subject: str, body: str, dedup_key: str, priority: str) -> Sent:
    """Deliver through HERALD's ``send``. The receipt is read, not trusted: a send
    that fell back to a non-durable inbox is reported unreachable."""
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        send,
    )
    from axiom.governance import Classification

    ctx = _herald_ctx()
    receipt = send(
        ctx, actor=ACTOR, recipient=recipient, payload=NotificationPayload(summary=subject[:240], body=body),
        classification=Classification.INTERNAL, priority=Priority(priority), intent="program.followup",
        dedup_key=dedup_key,
    )
    suppressed = receipt.outcome == "suppressed_duplicate"
    ok = receipt.outcome in ("succeeded", "suppressed_duplicate")
    return Sent(ok=ok, channel=receipt.channel_selected, durable=bool(receipt.durable) or suppressed,
                suppressed=suppressed, receipt_id=receipt.id, error=receipt.error)


_ctx_cache: Any = None


def _herald_ctx() -> Any:
    global _ctx_cache
    if _ctx_cache is None:
        from axiom.extensions.builtins.notifications.send import SendContext

        _ctx_cache = SendContext.default()
    return _ctx_cache


# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else "?"


def operator_for(state_dir: Any) -> str | None:
    """Who gets the last rung: $AXI_FOLLOWUP_OPERATOR if set (explicit configuration
    wins), else the program deputy."""
    import os
    from pathlib import Path

    from .model import ProgramError, load_program

    env = os.environ.get(OPERATOR_ENV, "").strip()
    if fm.valid_principal(env):
        return env
    try:
        data = load_program(Path(state_dir) / "program" / "data.json")
        deputy = data.deputy()
        if deputy and fm.valid_principal(deputy):
            return deputy
    except (ProgramError, OSError):
        pass
    return None


SUMMARY_MAX = 600


def _digest(check: Mapping[str, Any], limit: int) -> str:
    """The check, as one short clause for the line an inbox keeps."""
    if not check.get("ran") and not check.get("error"):
        return ""
    name = check.get("skill")
    if check.get("timed_out"):
        return f"check {name}: no answer"
    if not check.get("ran"):
        return f"check {name}: not run"
    verdict = "passed" if check.get("ok") else "FAILED"
    fields = ", ".join(f"{k}={v}" for k, v in (check.get("fields") or {}).items())
    text = f"check {name} {verdict}" + (f" ({fields})" if fields else "")
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."


def compose(row: Mapping[str, Any], rung: Mapping[str, Any], idx: int, check: Mapping[str, Any]) -> tuple[str, str]:
    """(summary, body) for one rung. Pure: the same inputs give the same bytes.

    The summary is one line and carries everything needed to act (what, the
    check's state, the next command, how to acknowledge), because the inbox
    channel keeps only the summary. The body is the same content laid out for a
    channel that carries one (email, chat).
    """
    kind = rung["kind"]
    total = len(row["ladder"])
    if kind == "reminder":
        head = f"Follow-up due: {row['what']}"
    elif kind == "flag":
        head = f"Follow-up not acknowledged by {row['owner']}: {row['what']}"
    else:
        head = f"Follow-up unanswered, please contact {row['owner']}: {row['what']}"
    fid = row["id"]
    tail = [f"next: {row['next_command']}"] if row.get("next_command") else []
    tail.append(f"ack: axi program followup ack --id {fid}")
    fixed = " | ".join([f"{head} [{fid}]", *tail])
    budget = SUMMARY_MAX - len(fixed) - 3
    digest = _digest(check, budget) if budget > 20 else ""
    summary = " | ".join([f"{head} [{fid}]", *([digest] if digest else []), *tail])

    lines = [
        f"{row['what']}",
        f"Follow-up {fid}  |  owner {row['owner']}  |  asked by {row['created_by']}  |  due {_iso(row['due_at'])}"
        f"  |  reminder {idx + 1} of {total}",
    ]
    rendered = check_mod.render(check)
    if rendered:
        lines += ["", rendered]
    if row.get("next_command"):
        lines += ["", "Next command:", f"  {row['next_command']}"]
    if kind != "reminder":
        lines += ["", f"{row['owner']} has not acknowledged this since it came due."]
    lines += ["", "Respond:", f"  {fm.next_command_hint(row)}"]
    return fm.scrub_text(summary)[0], fm.scrub_text("\n".join(lines))[0]


def _audience(row: Mapping[str, Any], audience: str, state_dir: Any) -> str | None:
    if audience == "owner":
        return row["owner"]
    if audience == "requester":
        return row["created_by"]
    return operator_for(state_dir)


def _advance(row: dict[str, Any], *, now: datetime, actor: str) -> dict[str, Any]:
    """Move to the next rung (or the horizon) and register the next one-shot."""
    nxt = row["rung"] + 1
    at = fm.rung_time(row["due_at"], row["ladder"], nxt) or row["expires_at"]
    state = "due" if row["state"] in ("open", "snoozed") else row["state"]
    updated = store.update(row["id"], rung=nxt, state=state, next_action_at=at) or row
    try:
        sid = clock.rearm(row["id"], row.get("schedule_id"), at)
        updated = store.update(row["id"], schedule_id=sid) or updated
        store.add_event(row["id"], "armed", actor, now=now, cycle=row["cycle"], rung=nxt,
                        detail={"at": _iso(at), "schedule_id": sid})
    except Exception as exc:  # noqa: BLE001 - the sweep re-arms a lost clock
        store.add_event(row["id"], "arm_failed", actor, now=now, cycle=row["cycle"], detail={"error": type(exc).__name__})
    return updated


def fire(
    followup_id: str, *, registry: Any, state_dir: Any, now: datetime | None = None, sender: Sender | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    sender = sender or herald_send
    row = store.get(followup_id)
    if row is None:
        return {"outcome": "gone"}
    if row["state"] in ("done", "acknowledged", "expired"):
        return {"outcome": "nothing_to_do", "state": row["state"]}
    at = row["next_action_at"]
    if at is not None and at > now + _TOLERANCE:
        # A snooze moved the time after this row was registered; the clock row was replaced,
        # but a stale one can still fire. Re-arm and do nothing.
        _arm_again(row, now)
        return {"outcome": "not_yet", "next_action_at": _iso(at)}

    cycle, idx = row["cycle"], row["rung"]
    if idx >= len(row["ladder"]):
        store.update(row["id"], state="expired", next_action_at=None, schedule_id=None)
        clock.disarm(row.get("schedule_id"))
        store.add_event(row["id"], "expired", ACTOR, now=now, cycle=cycle, detail={"at": _iso(now)})
        return {"outcome": "expired"}

    rung = row["ladder"][idx]
    delivered_key = f"delivered:{row['id']}:{cycle}:{idx}"
    if store.has_event(delivered_key):
        _advance(row, now=now, actor=ACTOR)
        return {"outcome": "advanced_after_delivery", "rung": idx}

    store.add_event(row["id"], "attempt", ACTOR, now=now, cycle=cycle, rung=idx, dedup_key=f"attempt:{row['id']}:{cycle}:{idx}")

    recipient = _audience(row, rung["audience"], state_dir)
    already = {e["detail"].get("recipient") for e in store.events(row["id"], kind="delivered", cycle=cycle)}
    if rung["audience"] != "owner" and (recipient is None or recipient == row["owner"] or recipient in already):
        why = "no_operator" if recipient is None else ("same_as_owner" if recipient == row["owner"] else "already_told")
        store.add_event(row["id"], "skipped", ACTOR, now=now, cycle=cycle, rung=idx,
                        detail={"audience": rung["audience"], "why": why}, dedup_key=delivered_key)
        _advance(row, now=now, actor=ACTOR)
        return {"outcome": "skipped", "why": why, "rung": idx}

    check = check_mod.run_check(row, registry, state_dir) if row.get("check_skill") else {"ran": False}
    if check.get("ran") or check.get("error"):
        store.add_event(row["id"], "check_ran", ACTOR, now=now, cycle=cycle, rung=idx, detail=_check_detail(check))
    subject, body = compose(row, rung, idx, check)
    priority = "normal" if rung["kind"] == "reminder" else "high"
    sent = sender(recipient, subject, body, f"followup:{row['id']}:{cycle}:{idx}", priority)  # type: ignore[arg-type]

    detail = {"audience": rung["audience"], "recipient": recipient, "channel": sent.channel, "reach": sent.reach,
              "receipt": sent.receipt_id, "suppressed_duplicate": sent.suppressed}
    if sent.reach == "unreachable":
        store.add_event(row["id"], "unreachable", ACTOR, now=now, cycle=cycle, rung=idx,
                        detail={**detail, "error": (sent.error or "")[:200]})
        _promote_unreachable(row, rung, idx, check, sent, state_dir=state_dir, sender=sender, now=now)
    store.add_event(row["id"], "delivered", ACTOR, now=now, cycle=cycle, rung=idx, detail=detail, dedup_key=delivered_key)
    _advance(row, now=now, actor=ACTOR)
    return {"outcome": "delivered", "rung": idx, "reach": sent.reach, "recipient": recipient}


def _promote_unreachable(row, rung, idx, check, sent, *, state_dir, sender, now) -> None:
    """The owner cannot be reached: tell the requester and the operator now, not 24 h later."""
    for audience in ("requester", "operator"):
        who = _audience(row, audience, state_dir)
        if not who or who == row["owner"]:
            continue
        flag = {"audience": audience, "kind": "flag"}
        subject, body = compose(row, flag, idx, check)
        body = f"{row['owner']} could not be reached on any channel, so this came to you.\n\n" + body
        s = sender(who, subject, body, f"followup:{row['id']}:{row['cycle']}:{idx}:promote:{audience}", "high")
        store.add_event(row["id"], "escalated", ACTOR, now=now, cycle=row["cycle"], rung=idx,
                        detail={"audience": audience, "recipient": who, "why": "owner_unreachable", "reach": s.reach})


def _check_detail(check: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("skill", "ran", "ok", "timed_out", "error", "fields", "errors", "redactions", "seconds")
    return {k: check[k] for k in keep if k in check}



def _arm_again(row: dict[str, Any], now: datetime) -> None:
    try:
        sid = clock.rearm(row["id"], row.get("schedule_id"), max(row["next_action_at"], now))
        store.update(row["id"], schedule_id=sid)
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not re-arm %s: %s", row["id"], type(exc).__name__)


# ---------------------------------------------------------------------------


def sweep(*, now: datetime | None = None) -> dict[str, Any]:
    """Re-arm lost clocks, expire stale follow-ups, mark never-delivered ones missed."""
    now = now or datetime.now(UTC)
    rearmed: list[str] = []
    expired: list[str] = []
    missed: list[str] = []
    for row in store.all_rows(states=fm.ACTIVE + ("acknowledged",)):
        fid = row["id"]
        if now >= row["expires_at"]:
            clock.disarm(row.get("schedule_id"))
            store.update(fid, state="expired", next_action_at=None, schedule_id=None)
            store.add_event(fid, "expired", ACTOR, now=now, cycle=row["cycle"], detail={"by": "sweep"})
            expired.append(fid)
            continue
        if row["state"] == "acknowledged":
            continue
        if not clock.is_active(row.get("schedule_id")):
            at = max(row["next_action_at"] or now, now)
            try:
                sid = clock.arm(fid, at)
                store.update(fid, schedule_id=sid, next_action_at=row["next_action_at"] or at)
                store.add_event(fid, "rearmed", ACTOR, now=now, cycle=row["cycle"], rung=row["rung"],
                                detail={"at": _iso(at), "schedule_id": sid})
                rearmed.append(fid)
            except Exception as exc:  # noqa: BLE001
                _log.warning("sweep could not re-arm %s: %s", fid, type(exc).__name__)
        if (now - row["due_at"]) > fm.MISSED_GRACE and not store.events(fid, kind="delivered", cycle=row["cycle"]):
            if store.add_event(fid, "missed", ACTOR, now=now, cycle=row["cycle"],
                               detail={"due_at": _iso(row["due_at"])}, dedup_key=f"missed:{fid}:{row['cycle']}"):
                missed.append(fid)
    return {"rearmed": rearmed, "expired": expired, "missed": missed,
            "quiet": not (rearmed or expired or missed)}


__all__ = ["ACTOR", "Sent", "compose", "fire", "herald_send", "operator_for", "sweep"]
