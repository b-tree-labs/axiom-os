# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The record filters a surface may ask for, parsed once for the HTTP route, the skill and the
exports (unit W3): entry types, meanings, the signer, a time window, one interval and a text.

A filter narrows what the caller may already read; it never widens the site. A malformed value
is a :class:`ValueError` whose message names the field, so a route can answer 400 and a skill
can fail with the same words.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from axiom.attest import query

__all__ = ["RecordFilters", "parse_filters"]


def _list(value: Any) -> tuple[str, ...] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    else:
        items = [str(v).strip() for v in value]
    items = [v for v in items if v]
    return tuple(items) if items else None


@dataclass(frozen=True)
class RecordFilters:
    entry_types: tuple[str, ...] | None = None
    meanings: tuple[str, ...] | None = None
    signer: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    interval: tuple[str, str] | None = None
    text: str | None = None

    def apply(
        self,
        logbook: str,
        site: str,
        *,
        from_seq: int = 1,
        before_seq: int | None = None,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """The records of ``logbook`` at ``site`` these filters keep, in chain order, read by the
        database from its indexes; with ``limit`` one keyset page (newest, or before or after a
        sequence)."""
        return query.records(
            logbook,
            site=site,
            from_seq=from_seq,
            before_seq=before_seq,
            after_seq=after_seq,
            limit=limit,
            entry_types=self.entry_types,
            meanings=self.meanings,
            signer=self.signer,
            since=self.since,
            until=self.until,
            interval=self.interval,
            text=self.text,
        )

    def any(self) -> bool:
        return any(v is not None for v in vars(self).values())


# Each surface's spelling of a filter, so one parser serves them all.
_TYPES = ("entry_type", "entry_types", "type", "types")
_MEANINGS = ("meaning", "meanings")
_SINCE = ("from", "since")
_UNTIL = ("to", "until")
_TEXT = ("q", "text")


def _first(raw: Mapping[str, Any], names: tuple[str, ...]) -> tuple[str | None, Any]:
    for name in names:
        value = raw.get(name)
        if value is not None and value != "" and value != []:
            return name, value
    return None, None


def parse_filters(raw: Mapping[str, Any]) -> RecordFilters:
    """Read the filters a caller spelled (``entry_type`` or ``type``, ``from`` or ``since``,
    ``q`` or ``text``, ...). Unknown keys are ignored: they are the caller's other parameters."""
    _, types = _first(raw, _TYPES)
    _, meanings = _first(raw, _MEANINGS)
    since_name, since = _first(raw, _SINCE)
    until_name, until = _first(raw, _UNTIL)
    _, interval = _first(raw, ("interval",))
    _, text = _first(raw, _TEXT)
    signer = raw.get("signer")
    return RecordFilters(
        entry_types=_list(types),
        meanings=_list(meanings),
        signer=str(signer).strip() if signer else None,
        since=query.parse_time(since, since_name or "from") if since else None,
        until=query.parse_time(until, until_name or "to") if until else None,
        interval=query.parse_interval(interval) if interval else None,
        text=str(text).strip() if text else None,
    )
