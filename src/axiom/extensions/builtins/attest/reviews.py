# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Review coverage of a log (#1817; spec-attestation, "Continuous review").

A **review** is a signed statement with meaning ``reviewed`` (ADR-274) in a reviews log: any log
that declares a type carrying that meaning. It names the log it reviewed in its ``logbook`` field
and, optionally, the range it examined in ``from_seq`` and ``to_seq``. A review that names no
range covers every record of the log signed before it.

:func:`coverage` answers in one query per table, at one site: how many reviews there are, the last
one, how many of the log's records a review covers, the ranges never reviewed, and the same per
month. A review counts only when ``may_read`` admits the reviews log that holds it: a reader who
may not read that log sees no review and no count of what is hidden (LD-1-A5's rule).

A review stands as the record model says it does after the records that act on it (#2243): one
whose thread holds a record meaning ``retracted`` does not count, and a ``correction`` supplement
restates its fields, the last record in the thread that states a field winning (design note L2,
section 6, the rule :func:`.changes.corrected` applies to every value). So a corrected range is
the range counted, and a review corrected to name another log counts for that log, not this one.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from . import registry, store
from .db_models import AttestRecord

REVIEWED = "reviewed"
#: The meaning of a record that withdraws the record it targets (spec-attestation's record model).
RETRACTED = "retracted"
#: A supplement for this reason restates the fields of the record it targets (unit L2).
CORRECTION = "correction"
#: The fields of a review that say what it covers.
_COVERS = ("logbook", "from_seq", "to_seq")
#: At most this many of the reviewed log's records are read; ``complete`` says whether all were.
MAX_RECORDS = 50_000


def reviews_logs() -> list[str]:
    """Every declared log with a type that carries the meaning ``reviewed``."""
    return sorted(b.id for b in registry.all_logbooks()
                  if any(REVIEWED in t.meanings for t in b.types))  # fmt: skip


def _seq(value: Any) -> int | None:
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return int(n) if n == n.to_integral_value() and n >= 1 else None


def _ranges(seqs: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in seqs:
        if out and out[-1][1] == n - 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def _acts(s: Any, site_id: str, logs: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Every record in ``logs`` at ``site_id`` that acts on another, grouped under the record at
    the root of its thread (a supplement of a supplement belongs to the first one's root), in
    chain order. Only acts that change what a review covers are read: corrections and
    retractions."""
    rows = s.execute(
        select(AttestRecord.attestation_id, AttestRecord.target, AttestRecord.record)
        .where(AttestRecord.site_id == site_id, AttestRecord.logbook.in_(logs),
               AttestRecord.target.is_not(None),
               AttestRecord.meaning.in_([RETRACTED, "corrected"]))
        .order_by(AttestRecord.logbook, AttestRecord.seq.asc())
    ).all()  # fmt: skip
    root: dict[str, str] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for attestation_id, target, record in rows:
        top = root.get(target, target)
        root[attestation_id] = top
        out.setdefault(top, []).append(record or {})
    return out


def _standing(record: dict[str, Any], acts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A review's covering fields after the records that act on it, or None when retracted."""
    fields = dict((record.get("content") or {}).get("fields") or {})
    for act in acts:
        if act.get("meaning") == RETRACTED:
            return None
        if act.get("kind") == "supplement" and act.get("reason") == CORRECTION:
            stated = (act.get("content") or {}).get("fields") or {}
            fields.update({k: stated[k] for k in _COVERS if stated.get(k) not in (None, "")})
    return fields


def coverage(site_id: str, logbook: str, *, may_read: Callable[[str], bool]) -> dict[str, Any]:
    """Review coverage of ``logbook`` at ``site_id``, counting only reviews ``may_read`` admits."""
    registry.get(logbook)  # an undeclared log is refused by the caller's LogbookError handling
    readable = [b for b in reviews_logs() if may_read(b)]
    with store.session_scope() as s:
        recs = s.execute(
            select(AttestRecord.seq, AttestRecord.recorded_at)
            .where(AttestRecord.site_id == site_id, AttestRecord.logbook == logbook)
            .order_by(AttestRecord.seq.asc())
            .limit(MAX_RECORDS + 1)
        ).all()
        found, acts = [], {}
        if readable:
            acts = _acts(s, site_id, readable)
            named = AttestRecord.record["content"]["fields"]["logbook"].astext
            # A review names this log, or a correction may have made it name this log.
            which = named == logbook
            if acts:
                which = which | AttestRecord.attestation_id.in_(list(acts))
            found = s.execute(
                select(AttestRecord.attestation_id, AttestRecord.logbook, AttestRecord.signer,
                       AttestRecord.recorded_at, AttestRecord.record)
                .where(AttestRecord.site_id == site_id, AttestRecord.logbook.in_(readable),
                       AttestRecord.kind == "statement", AttestRecord.meaning == REVIEWED, which)
                .order_by(AttestRecord.recorded_at.asc(), AttestRecord.seq.asc())
            ).all()  # fmt: skip
    rows, retracted = [], 0
    for row in found:
        fields = _standing(row[4] or {}, acts.get(row[0], []))
        if fields is None:
            retracted += (((row[4] or {}).get("content") or {}).get("fields") or {}).get(
                "logbook"
            ) == logbook
        elif fields.get("logbook") == logbook:
            rows.append((*row[:4], fields))
    complete = len(recs) <= MAX_RECORDS
    recs = recs[:MAX_RECORDS]
    at = {int(seq): when for seq, when in recs}
    covered: set[int] = set()
    for _id, _log, _signer, when, fields in rows:
        lo, hi = _seq(fields.get("from_seq")), _seq(fields.get("to_seq"))
        if lo is not None and hi is not None and lo <= hi:
            covered.update(n for n in at if lo <= n <= hi)
        elif lo is None and hi is None:
            covered.update(n for n, signed in at.items() if signed <= when)
    last = rows[-1] if rows else None
    periods: dict[str, dict[str, Any]] = {}
    for n, when in at.items():
        p = periods.setdefault(when.strftime("%Y-%m"), {"period": when.strftime("%Y-%m"),
                                                        "records": 0, "reviewed": 0})  # fmt: skip
        p["records"] += 1
        p["reviewed"] += n in covered
    return {
        "site_id": site_id,
        "logbook": logbook,
        "reviews": len(rows),
        # Reviews of this log later retracted, from the reviews logs the caller may read.
        "retracted": retracted,
        "last_review": None if last is None else {
            "attestation_id": last[0], "reviews_log": last[1], "signer": last[2],
            "at": last[3].isoformat(),
        },
        "records": len(at),
        "reviewed": len(covered),
        "never_reviewed": _ranges(sorted(set(at) - covered)),
        "periods": sorted(periods.values(), key=lambda p: p["period"]),
        "complete": complete,
    }  # fmt: skip


__all__ = ["REVIEWED", "coverage", "reviews_logs"]
