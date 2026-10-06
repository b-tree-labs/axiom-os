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
    return s.execute(
        text(
            "SELECT number, opened_at FROM attest_intervals WHERE site_id = :site AND "
            "logbook = :logbook AND kind = :kind AND closed_at IS NULL"
            + (" FOR UPDATE" if lock else "")
        ),
        {"site": site, "logbook": logbook, "kind": kind},
    ).one_or_none()


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


__all__ = ["enter", "open_interval", "register_seed", "reset_seeds"]
