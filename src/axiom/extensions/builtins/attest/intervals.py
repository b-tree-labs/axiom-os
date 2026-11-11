# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Intervals a logbook's entries belong to, e.g. runs (spec-attestation).

A logbook declares ``[interval.<kind>]`` with the entry types that open and close
it. Signing an opener starts the next numbered interval; signing a closer ends
the open one. A type with ``requires_interval = "<kind>"`` signs only while one
is open; ``"none_open"`` signs only while none of the logbook's intervals is.

Every record signed under these rules carries ``interval`` in its signed
content: ``{kind, number}``, plus ``event: opened|closed`` on the record that
opened or closed it. ``attest_intervals`` is a projection written in the same
transaction, so it cannot disagree with the chain.

Numbering continues from the highest number used; the first interval at a
site starts from a seed the extension that owns the site's configuration
registers (:func:`register_seed`), else 1.

:func:`listing` reads the projection back for a surface (unit W3): every
interval with when it opened and closed, how long it lasted, how many records
and missed obligation events fell inside it, and the gaps between one
interval's close and the next's open. The server computes the durations and
gaps; a page never does that from its own clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import text

from . import store
from .logbooks import NONE_OPEN, EntryType, Logbook

_seeds: dict[tuple[str, str], Callable[[str], int]] = {}


def register_seed(logbook: str, kind: str, seed: Callable[[str], int]) -> None:
    """Where interval numbering starts at a site (e.g. continuing a paper log)."""
    _seeds[(logbook, kind)] = seed


def reset_seeds() -> None:
    _seeds.clear()


def _open_row(s: Any, site: str, logbook: str, kind: str, *, lock: bool) -> Any:
    # Both statements are literals. ``FOR UPDATE`` is a clause, not a value,
    # so it cannot be a bound parameter; the two queries are the two cases.
    statement = (
        text(
            "SELECT number, opened_at FROM attest_intervals WHERE site_id = :site AND "
            "logbook = :logbook AND kind = :kind AND closed_at IS NULL FOR UPDATE"
        )
        if lock
        else text(
            "SELECT number, opened_at FROM attest_intervals WHERE site_id = :site AND "
            "logbook = :logbook AND kind = :kind AND closed_at IS NULL"
        )
    )
    return s.execute(
        statement,
        {"site": site, "logbook": logbook, "kind": kind},
    ).one_or_none()


def _iso(value: Any) -> str | None:
    return (
        value.isoformat()
        if hasattr(value, "isoformat")
        else (None if value is None else str(value))
    )


def listing(site: str, logbook_id: str, *, kind: str | None = None) -> dict[str, Any]:
    """Every interval of ``logbook_id`` at ``site`` (optionally one ``kind``), oldest first,
    with its counts, and the gaps between consecutive intervals of a kind.

    ``kinds`` is what the logbook declares, in its order. A logbook that declares no
    intervals, or one this node does not know, gives empty lists, never an error: the
    surface says "declares no intervals" from that.
    """
    from . import registry
    from .logbooks import LogbookError

    try:
        declared = list(registry.get(logbook_id).intervals)
    except LogbookError:
        declared = []
    kinds = [k for k in declared if kind is None or k == kind]
    if kind is not None and kind not in declared:
        return {"kinds": kinds, "intervals": [], "gaps": []}

    # Both statements are literals (as in ``_open_row``): the two queries are the two cases.
    every_kind = text(
        "SELECT kind, number, opened_by, opened_at, closed_by, closed_at FROM attest_intervals "
        "WHERE site_id = :site AND logbook = :logbook ORDER BY kind, number"
    )
    one_kind = text(
        "SELECT kind, number, opened_by, opened_at, closed_by, closed_at FROM attest_intervals "
        "WHERE site_id = :site AND logbook = :logbook AND kind = :kind ORDER BY kind, number"
    )
    with store.session_scope() as s:
        rows = s.execute(
            one_kind if kind else every_kind,
            {"site": site, "logbook": logbook_id, **({"kind": kind} if kind else {})},
        ).all()
        counted = s.execute(
            text(
                "SELECT record->'interval'->>'kind' AS kind, "
                "(record->'interval'->>'number')::bigint AS number, count(*) AS n "
                "FROM attest_records WHERE site_id = :site AND logbook = :logbook "
                "AND record->'interval' IS NOT NULL GROUP BY 1, 2"
            ),
            {"site": site, "logbook": logbook_id},
        ).all()
        missed = s.execute(
            text(
                "SELECT interval_kind AS kind, interval_number AS number, count(*) AS n "
                "FROM attest_obligation_events WHERE site_id = :site AND logbook = :logbook "
                "AND state = 'missed' GROUP BY 1, 2"
            ),
            {"site": site, "logbook": logbook_id},
        ).all()
    records = {(r.kind, int(r.number)): int(r.n) for r in counted}
    misses = {(r.kind, int(r.number)): int(r.n) for r in missed}

    out: list[dict[str, Any]] = []
    for r in rows:
        key = (r.kind, int(r.number))
        closed = r.closed_at is not None
        duration = (r.closed_at - r.opened_at).total_seconds() if closed else None
        out.append(
            {
                "kind": r.kind,
                "number": int(r.number),
                "state": "closed" if closed else "open",
                "opened_by": r.opened_by,
                "opened_at": _iso(r.opened_at),
                "closed_by": r.closed_by,
                "closed_at": _iso(r.closed_at),
                "duration_seconds": duration,
                "records": records.get(key, 0),
                "missed": misses.get(key, 0),
            }
        )
    gaps: list[dict[str, Any]] = []
    for prev, nxt in zip(rows, rows[1:], strict=False):
        if prev.kind != nxt.kind or prev.closed_at is None:
            continue
        gaps.append(
            {
                "kind": prev.kind,
                "after": int(prev.number),
                "before": int(nxt.number),
                "from": _iso(prev.closed_at),
                "to": _iso(nxt.opened_at),
                "seconds": (nxt.opened_at - prev.closed_at).total_seconds(),
            }
        )
    return {"kinds": kinds, "intervals": out, "gaps": gaps}


def open_interval(site: str, logbook: str, kind: str) -> dict[str, Any] | None:
    with store.session_scope() as s:
        row = _open_row(s, site, logbook, kind, lock=False)
        return None if row is None else {"number": int(row.number), "opened_at": row.opened_at}


def enter(
    s: Any, logbook: Logbook, et: EntryType, site: str, attestation_id: str, now: datetime
) -> dict[str, Any] | None:
    """Check ``et``'s interval rule inside the signing transaction and apply
    its effect. Returns the ``interval`` to put in the record, or None."""
    from .service import AttestRefused

    rule = et.requires_interval
    opens = [k for k, d in logbook.intervals.items() if et.id in d.opens]
    closes = [k for k, d in logbook.intervals.items() if et.id in d.closes]
    if rule is None and not opens and not closes:
        return None

    if rule == NONE_OPEN:
        busy = [k for k in logbook.intervals if _open_row(s, site, logbook.id, k, lock=True)]
        if busy:
            raise AttestRefused(f"a {busy[0]} is already open at {site}; close it first")
    elif rule is not None:
        if _open_row(s, site, logbook.id, rule, lock=True) is None:
            raise AttestRefused(f"no {rule} is open at {site}; {et.id} belongs inside one")

    if opens:
        kind = opens[0]
        if _open_row(s, site, logbook.id, kind, lock=True) is not None:
            raise AttestRefused(f"a {kind} is already open at {site}")
        top = s.execute(
            text(
                "SELECT max(number) FROM attest_intervals "
                "WHERE site_id = :site AND logbook = :logbook AND kind = :kind"
            ),
            {"site": site, "logbook": logbook.id, "kind": kind},
        ).scalar()
        if top is not None:
            number = int(top) + 1
        else:
            seed = _seeds.get((logbook.id, kind))
            number = int(seed(site)) if seed else 1
        s.execute(
            text(
                "INSERT INTO attest_intervals (site_id, logbook, kind, number, opened_by, opened_at) "
                "VALUES (:site, :logbook, :kind, :number, :by, :at)"
            ),
            {
                "site": site,
                "logbook": logbook.id,
                "kind": kind,
                "number": number,
                "by": attestation_id,
                "at": now,
            },
        )
        return {"kind": kind, "number": number, "event": "opened"}

    if closes:
        kind = closes[0]
        row = _open_row(s, site, logbook.id, kind, lock=True)
        if row is None:
            raise AttestRefused(f"no {kind} is open at {site} to close")
        s.execute(
            text(
                "UPDATE attest_intervals SET closed_by = :by, closed_at = :at WHERE site_id = :site "
                "AND logbook = :logbook AND kind = :kind AND number = :number"
            ),
            {
                "by": attestation_id,
                "at": now,
                "site": site,
                "logbook": logbook.id,
                "kind": kind,
                "number": row.number,
            },
        )
        return {"kind": kind, "number": int(row.number), "event": "closed"}

    if rule is not None and rule != NONE_OPEN:
        row = _open_row(s, site, logbook.id, rule, lock=False)
        return {"kind": rule, "number": int(row.number)}
    return None


__all__ = ["enter", "listing", "open_interval", "register_seed", "reset_seeds"]
