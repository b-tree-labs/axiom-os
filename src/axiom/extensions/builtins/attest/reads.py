# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reads of signed records are recorded at the door, on the one action audit chain (unit X2).

Every door that hands out a record calls one of two functions here, so a read through any
surface leaves the same record: who (the principal and, for a session, the person), when, which
surface, which logbook at which site, which record ids or how many, and nothing of the content.

- A skill registered as an audited read (``attest.show``, ``attest.verify``, ``attest.export``)
  calls :func:`note_access`; the skill registry's own audit record for that invocation then
  carries the ``access`` key (the ``note_change`` pattern of the change log).
- A door with no skill around it (the HTTP routes, the search provider) calls
  :func:`record_access`, which appends the record itself as ``attest.read.<what>``.

A refused read is recorded too, once at the door (:func:`record_denial`, #1885): the person,
the door, the log and site asked for, and the refusal's code, with no ids and no content.

Failure isolation is absolute: a read never fails because its record could not be written. The
failure is counted (:func:`failures`) and logged. Reading the access log (``attest.access``) is
not itself recorded, which is what keeps it from recursing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from axiom.infra import audit_trail

log = logging.getLogger("axiom.attest.reads")

#: The ``skill`` field of a record a door writes itself: ``attest.read.records`` and so on.
DOOR_PREFIX = "attest.read."

#: What a read may be: a list, one record, a verification, an open stream, a search, an export,
#: or the intervals a log ran with their entry counts (the e2e suite, #1818, found it unrecorded).
WHATS = ("records", "record", "verify", "stream", "search", "export", "intervals", "obligations")

_failures = 0


def failures() -> int:
    """How many access records could not be written this process."""
    return _failures


def reset_failures() -> None:
    global _failures
    _failures = 0


def access(
    *,
    what: str,
    site: str | None,
    logbook: str | None,
    ids: Any = (),
    count: int | None = None,
    surface: str | None = None,
    person: str | None = None,
) -> dict[str, Any]:
    """The ``access`` key, one shape for every door. Ids only, never content."""
    if what not in WHATS:
        raise ValueError(f"unknown read {what!r}; one of {', '.join(WHATS)}")
    id_list = [str(i) for i in (ids or ()) if i]
    return {
        "what": what,
        "logbook": logbook,
        "site": site,
        "ids": id_list,
        "count": len(id_list) if count is None else int(count),
        "surface": surface,
        "person": person,
    }


def note_access(
    *,
    what: str,
    site: str | None,
    logbook: str | None,
    ids: Any = (),
    count: int | None = None,
    surface: str | None = None,
    person: str | None = None,
) -> None:
    """Say what the running read skill read; the registry's audit record carries it."""
    try:
        audit_trail.note_access(
            access(
                what=what,
                site=site,
                logbook=logbook,
                ids=ids,
                count=count,
                surface=surface,
                person=person,
            )  # fmt: skip
        )
    except Exception:  # noqa: BLE001 - a note never fails a read
        global _failures
        _failures += 1
        log.debug("attest: access note dropped", exc_info=True)


def record_access(
    *,
    state_dir: Path,
    principal: str,
    what: str,
    site: str | None,
    logbook: str | None,
    ids: Any = (),
    count: int | None = None,
    surface: str | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Append one access record for a door with no skill around it. Never raises."""
    global _failures
    try:
        audit_trail.record_invocation(
            state_dir=Path(state_dir),
            skill=DOOR_PREFIX + what,
            params=params if params is not None else {"logbook": logbook},
            principal=principal or audit_trail.DEFAULT_PRINCIPAL,
            site=site,
            outcome="ok",
            errors_count=0,
            access=access(
                what=what,
                site=site,
                logbook=logbook,
                ids=ids,
                count=count,
                surface=surface,
                person=principal,
            ),  # fmt: skip
        )
    except Exception as exc:  # noqa: BLE001 - isolation is the contract
        _failures += 1
        log.warning("attest: access record for %s not written: %s", what, exc)


def record_denial(
    *,
    state_dir: Path,
    principal: str,
    what: str,
    site: str | None,
    logbook: str | None,
    code: str | None,
    status: int | None = None,
    surface: str | None = None,
) -> None:
    """Append one record of a refused read (#1885): who asked, at which door, for which log at
    which site, and why it was refused (``refused``, the answer's code). Nothing was read, so the
    record carries no ids and a count of zero. Never raises."""
    global _failures
    try:
        audit_trail.record_invocation(
            state_dir=Path(state_dir),
            skill=DOOR_PREFIX + what,
            params={"logbook": logbook, "site": site},
            principal=principal or audit_trail.DEFAULT_PRINCIPAL,
            site=site,
            outcome="denied",
            errors_count=1,
            access={
                **access(what=what, site=site, logbook=logbook, count=0, surface=surface,
                         person=principal),  # fmt: skip
                "refused": code,
                "status": status,
            },
        )
    except Exception as exc:  # noqa: BLE001 - isolation is the contract
        _failures += 1
        log.warning("attest: denial record for %s not written: %s", what, exc)


def is_access_record(rec: dict[str, Any]) -> bool:
    skill = str(rec.get("skill") or "")
    return bool(rec.get("access")) and (
        skill.startswith(DOOR_PREFIX) or skill.startswith("attest.")
    )


def access_records(
    state_dir: Path, *, logbook: str | None = None, since: Any = None, limit: int = 50
) -> list[dict[str, Any]]:
    """The reads on the chain, newest first: who read what, when, through which surface."""
    from axiom.policy.action_ledger import parse_when

    floor = parse_when(since) if since else None
    out: list[dict[str, Any]] = []
    for rec in reversed(audit_trail.read_chain(audit_trail.audit_path(Path(state_dir)))):
        if not is_access_record(rec):
            continue
        a = rec["access"]
        if logbook and a.get("logbook") != logbook:
            continue
        if floor is not None:
            try:
                at = datetime.fromisoformat(str(rec.get("ts")))
            except ValueError:
                continue
            if at < floor:
                continue
        out.append(
            {
                "at": rec.get("ts"),
                "principal": rec.get("principal"),
                "person": a.get("person"),
                "what": a.get("what"),
                "logbook": a.get("logbook"),
                "site": a.get("site") or rec.get("site"),
                "ids": list(a.get("ids") or []),
                "count": a.get("count"),
                "surface": a.get("surface"),
                "skill": rec.get("skill"),
                "seq": rec.get("seq"),
                "outcome": rec.get("outcome"),
                "refused": a.get("refused"),
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


__all__ = [
    "DOOR_PREFIX",
    "WHATS",
    "access",
    "access_records",
    "failures",
    "is_access_record",
    "note_access",
    "record_access",
    "record_denial",
    "reset_failures",
]
