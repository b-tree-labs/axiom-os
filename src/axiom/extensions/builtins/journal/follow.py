# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tail (filed as "Follow"): a live tail of a lane, on the web stream and the command line.

Logbook unit J3, the Axiom half, to the approved Tail design (appkit
``docs/design/proposed-2026-11-11/tail/README.md``, master design D48). What this module adds to
J1's stream and J2's heartbeat:

- **Access** at subscribe time (a lane the person may not read is refused with "Not available to
  you." and nothing else) and on every labelled row (``rights.visible``, as before).
- **Redaction** before the wire (:mod:`.redact`): at write and on every way out.
- **One recorded read** per subscription, with its range (J2, the X2 rule), from either surface.
- **Backpressure**: the live queue is bounded. A reader that falls behind is resynced from the
  store, never silently skipped; a resume further back than :data:`REPLAY_MAX` rows sends the newest
  :data:`REPLAY_MAX` after one ``gap`` event that says where it resumed (LB-J3-A9).
- **The CLI tail**: ``axi log timeline`` (and ``axi log tail --lane``) prints the Tail's columns;
  ``-f`` follows by keyset polling and prints a lost stream, never silently (LB-J3-A17).

A view, never a store: nothing here keeps rows beyond one bounded page.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from axiom.infra.skills import SkillResult

from . import rights, service
from .redact import shown

log = logging.getLogger("axiom.journal.follow")

#: Rows fetched per page when replaying or following.
PAGE = 200
#: The most rows a resume replays before it says there is a gap.
REPLAY_MAX = 2000
#: Live rows held for a slow reader before it is resynced from the store.
QUEUE_MAX = 256
#: How often the CLI asks for new rows while following.
POLL_SECONDS = 1.0
NOT_AVAILABLE = "Not available to you."

_sleep = time.sleep


def _state_dir():
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


# -- the read record -------------------------------------------------------------------------


def record_read(*, handle: str, site: str | None, lanes: list[str], subject: str | None,
                after: int | None, surface: str, state_dir: Any = None) -> None:  # fmt: skip
    """One subscription is one read of the journal, recorded once with its range and never a
    row. A failure to record is logged and never fails the read."""
    from axiom.infra import audit_trail

    try:
        audit_trail.record_invocation(
            state_dir=state_dir if state_dir is not None else _state_dir(),
            skill="journal.read.stream",
            params={"lanes": lanes, "subject": subject, "after": after},
            principal=handle,
            site=site,
            outcome="ok",
            errors_count=0,
            access={"what": "stream", "lanes": lanes, "subject": subject, "after": after,
                    "site": site, "surface": surface, "person": handle},
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - a read never fails because its record could not be written
        log.warning("the journal stream read was not recorded", exc_info=True)


# -- replay with a stated gap ----------------------------------------------------------------


def _narrow(params: dict[str, Any]) -> dict[str, Any]:
    """The Timeline's narrowing filters from a skill's params (mine, agent, levels)."""
    return {"mine": params.get("mine") or False, "agent": params.get("agent") or None,
            "levels": params.get("levels") or params.get("level") or None}  # fmt: skip


def subscribable(caller: rights.Caller, lanes: Any) -> list[str]:
    """The lanes a subscription may carry; empty means "Not available to you."."""
    return service.query(caller, lanes=lanes, limit=1)["lanes"]


def replay(caller: rights.Caller, *, after: int, lanes: list[str], subject: str | None,
           narrow: dict[str, Any] | None = None) -> Iterator[tuple[str, dict[str, Any]]]:  # fmt: skip
    """``("row", row)`` for every visible row after ``after``, oldest first; when more than
    :data:`REPLAY_MAX` are waiting, one ``("gap", {after, resumed_at})`` first and then the newest
    :data:`REPLAY_MAX`. Bounded: at most :data:`REPLAY_MAX` rows are held."""
    window: list[dict[str, Any]] = []
    cursor: str | None = None
    complete = False
    while len(window) < REPLAY_MAX:
        page = service.query(caller, lanes=lanes, subject=subject, cursor=cursor,
                             limit=min(PAGE, REPLAY_MAX - len(window)), **(narrow or {}))  # fmt: skip
        for row in page["rows"]:
            if row["seq"] <= after:
                complete = True
                break
            window.append(row)
        if complete or not page["next_cursor"]:
            complete = True
            break
        cursor = page["next_cursor"]
    if not window:
        return
    oldest = window[-1]["seq"]
    if not complete:
        probe = service.after(caller, after=after, lanes=lanes, subject=subject, limit=1,
                              **(narrow or {}))  # fmt: skip
        if probe["rows"] and probe["rows"][0]["seq"] < oldest:
            yield "gap", {"after": after, "resumed_at": oldest}
    for row in reversed(window):
        yield "row", row


# -- the CLI tail ----------------------------------------------------------------------------


def _when(at: str, today: datetime) -> str:
    try:
        t = datetime.fromisoformat(str(at)).astimezone()
    except ValueError:
        return str(at)
    return t.strftime("%H:%M:%S") if t.date() == today.date() else t.strftime("%m-%d %H:%M:%S")


def line(row: dict[str, Any], today: datetime | None = None) -> str:
    """One row in the Tail's columns: time, lane, actor, the line, the record number."""
    today = today or datetime.now().astimezone()
    # Two spaces between fields, never padded to a width: a followed line is printed as it
    # arrives, so no column can be sized to the widest value (cli_format's tables need them all).
    fields = [_when(row["at"], today), row["lane"], row.get("actor") or "", row.get("summary") or "",
              f"#{row['seq']}"]  # fmt: skip
    return "  ".join(f for f in fields if f)


def tail(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """``journal.tail``: print the last rows of a lane, then (``follow``) every new one until
    interrupted. A lane the person may not read prints "not available to you" and fails."""
    out, err = sys.stdout, sys.stderr
    caller = rights.caller_from_ctx(ctx)
    lanes = params.get("lanes") or params.get("lane") or None
    subject = params.get("run") or params.get("subject") or None
    limit = max(1, min(int(params.get("limit") or 20), service.MAX_LIMIT))
    narrow = _narrow(params)
    try:
        allowed = subscribable(caller, lanes)
        service.RowFilter.of(caller=caller, **narrow)
    except ValueError as exc:
        return SkillResult(ok=False, value=None, errors=[str(exc)])
    if not allowed:
        print("not available to you", file=err)
        return SkillResult(ok=False, value=None, errors=[])
    page = service.query(caller, lanes=allowed, subject=subject, limit=limit, **narrow)
    rows = list(reversed(page["rows"]))
    for row in rows:
        print(line(row), file=out)
    last = rows[-1]["seq"] if rows else newest_seq(caller, allowed, subject)
    if not params.get("follow"):
        return SkillResult(ok=True, value={"lanes": allowed, "last": last, "rows": len(rows)})
    record_read(handle=caller.handle, site=None, lanes=sorted(allowed), subject=subject,
                after=last, surface="cli", state_dir=_state_dir())  # fmt: skip
    seen = datetime.now().astimezone()
    lost = False
    try:
        while True:
            try:
                got = service.after(caller, after=last, lanes=allowed, subject=subject, limit=PAGE,
                                    **narrow)  # fmt: skip
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - a lost stream is printed and retried, never silent
                if not lost:
                    print(f"stream lost, last seen {seen:%H:%M:%S}; reconnecting", file=err)
                    lost = True
                _sleep(POLL_SECONDS)
                continue
            if lost:
                print("reconnected", file=err)
                lost = False
            seen = datetime.now().astimezone()
            for row in got["rows"]:
                print(line(row), file=out, flush=True)
            last = got["last"]
            if not got["more"]:
                _sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    return SkillResult(ok=True, value={"lanes": allowed, "last": last})


def newest_seq(caller: rights.Caller, lanes: list[str], subject: str | None) -> int:
    """Where following starts when nothing is shown: after the newest row of any kind, so the
    tail never replays history it did not print."""
    page = service.query(caller, lanes=lanes, subject=subject, limit=1)
    return page["rows"][0]["seq"] if page["rows"] else 0


__all__ = [
    "NOT_AVAILABLE",
    "PAGE",
    "POLL_SECONDS",
    "QUEUE_MAX",
    "REPLAY_MAX",
    "line",
    "newest_seq",
    "record_read",
    "replay",
    "shown",
    "subscribable",
    "tail",
]
