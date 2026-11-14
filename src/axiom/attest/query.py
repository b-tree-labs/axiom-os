# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read signed records of one chain, filtered (F1 design note, question 5; unit W3).

Every filter is answered by the database from its indexes (``service.records_where``): nothing
is read and then filtered here. Records come back as stored, in ``seq`` order, so they can be
handed to ``verify_chain``. Filters AND together; one that matches nothing gives an empty list,
never an error. Time bounds are inclusive instants (a naive bound is UTC). A page is a keyset on
``seq``: ``before_seq`` for older records, ``after_seq`` for newer, ``limit`` capped by the
caller. This is the one filter function behind ``GET /{logbook}/records``, ``attest.show`` and
the exports.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from axiom.extensions.builtins.attest import service

from ._site import site_or_refuse

__all__ = ["_in_interval", "parse_interval", "parse_time", "records"]


def parse_time(value: str, name: str = "time") -> datetime:
    """An ISO 8601 bound. A naive value is UTC; anything else is refused with the field's name."""
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO 8601 time, e.g. 2026-10-01T08:00:00Z") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_interval(value: str) -> tuple[str, str]:
    """``kind:number``, as a record's ``interval`` names it."""
    kind, sep, number = str(value).partition(":")
    if not sep or not kind.strip() or not number.strip():
        raise ValueError("interval must be kind:number, e.g. run:12")
    return kind.strip(), number.strip()


def _in_interval(record: dict[str, Any], interval: tuple[str, Any]) -> bool:
    """The F1 rule an interval filter follows, kept as the reference the SQL mirrors: the kind
    must match and the number compares as text, so ``12`` and ``"12"`` name the same interval."""
    kind, number = interval
    got = record.get("interval") or {}
    return got.get("kind") == kind and str(got.get("number")) == str(number)


def records(
    logbook: str,
    *,
    site: str | None = None,
    interval: tuple[str, Any] | None = None,
    meanings: Iterable[str] | None = None,
    text: str | None = None,
    from_seq: int = 1,
    entry_types: Iterable[str] | None = None,
    signer: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_seq: int | None = None,
    after_seq: int | None = None,
    limit: int | None = None,
    order: str = "seq",
    before_key: Any = None,
    after_key: Any = None,
) -> list[dict[str, Any]]:
    return service.records_where(
        site_or_refuse(site),
        logbook,
        from_seq=from_seq,
        before_seq=before_seq,
        after_seq=after_seq,
        limit=limit,
        entry_types=entry_types,
        meanings=meanings,
        signer=signer,
        since=since,
        until=until,
        interval=interval,
        text_=text,
        order=order,
        before_key=before_key,
        after_key=after_key,
    )
