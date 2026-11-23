# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Summary-first retrieval over archived periods (unit LD-8, ADR-251 D7).

A retired period (unit LD-5: archived as a verified package, its hot copy reclaimed by a person)
keeps exactly one chunk in the log's index (unit LD-2's table, named in ``index`` only): the
seal's, with the log's labels and ``kind = seal``. Its vector is the seal's own (title and summary); its text-search
column is the seal's words together with the lexemes of every record of the period whose content
still exists, so a query that matches an archived entry finds the summary that stands for it. No
row, vector or text is kept for the period's entries themselves.

Consolidation is lossless and compaction lossy (spec-memory-compaction): the package is the
escape hatch. :func:`rehydrate` reads the period from its package through a door that admitted
the log (and so wrote its read record, naming the archive as the source), and indexes its
records again as **warm** rows: the same table, the same labels, the same ``(model, dim)``, with
``warm_until`` set to now plus the declared window. A warm row past its window is never ranked
and is reaped at the next refresh (:func:`compact`) or by :func:`reap`.

There is no second search. :func:`axiom.extensions.builtins.attest.index.search` ranks the
summary, the warm rows and the hot rows together after the one check, and :func:`mark` says of
each result whether it came from an archived period, which, and whether it is a summary.

**Removed records** (unit LD-6's tombstones): a record a committed tombstone hides, or whose
content an erased one removed, is never folded into a summary, never warmed and never served
from the package; rehydrate answers it as removed. :func:`_removed_ids` is the one place that
says which (``retention.hidden_ranges`` and gold's content-less rows), and the summary is folded
again whenever that set changes. A period whose tombstone was erased (archive status
``tombstoned``, its package gone) keeps its summary, folded from what remains.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import archives, gold, index, store

#: What this indexer reads: the gold view (and the package, whose rows equal gold's, LD-5-A7).
INDEX_SOURCES: tuple[str, ...] = (f"gold.{gold.VIEW}",)

#: The declared warm window, in hours (a node setting). Never longer than :data:`WARM_MAX`.
WARM_ENV = "AXIOM_LOG_INDEX_WARM_HOURS"
WARM_DEFAULT = timedelta(days=7)
#: No warm row is kept longer than this, whatever is declared: fine vectors are not kept "in case".
WARM_MAX = timedelta(days=30)
#: The statuses whose period reads come from the package; only a retired period is compacted.
RETIRED = "retired"
#: An archived period whose tombstone erased its content and its package (unit LD-6): still
#: compacted to its summary, folded from what remains; nothing is read from a package.
TOMBSTONED = "tombstoned"
COMPACTED = (RETIRED, TOMBSTONED)
#: Gold rows read per batch while folding a period's lexemes into its summary.
BATCH = 200


def _now() -> datetime:
    return datetime.now(UTC)


def warm_window() -> timedelta:
    """How long a rehydrated period stays warm: :data:`WARM_ENV` hours, else seven days; at most
    :data:`WARM_MAX`. A caller cannot ask for longer."""
    raw = os.environ.get(WARM_ENV, "").strip()
    try:
        hours = float(raw) if raw else 0.0
    except ValueError:
        hours = 0.0
    if hours <= 0:
        return WARM_DEFAULT
    return min(timedelta(hours=hours), WARM_MAX)


# -- the periods ---------------------------------------------------------------------------------


def _periods(s: Any, site: str, logbook: str, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    if not archives._table_exists(s):  # noqa: SLF001 - a node before LD-5's table has no archive
        return []
    from .db_models import AttestArchive

    rows = s.execute(select(AttestArchive).where(
        AttestArchive.site_id == site, AttestArchive.logbook == logbook,
        AttestArchive.status.in_(statuses)).order_by(AttestArchive.from_seq)).scalars()  # fmt: skip
    return [{"archive_id": a.archive_id, "site_id": a.site_id, "logbook": a.logbook,
             "seal_id": a.seal_id, "from_seq": int(a.from_seq), "to_seq": int(a.to_seq),
             "last_seq": int(a.last_seq), "status": a.status, "package_digest": a.package_digest}
            for a in rows]  # fmt: skip


def periods(site: str, logbook: str) -> list[dict[str, Any]]:
    """The archived and retired periods of one log (whose reads come from their packages)."""
    with store.session_scope() as s:
        return _periods(s, site, logbook, archives.SERVING)


def period_of(site: str, logbook: str, archive_id: str) -> dict[str, Any] | None:
    """One archived period of this log, or None: an archive of another log or site is not
    named here, so asking for it answers exactly as asking for one that does not exist."""
    return next((p for p in periods(site, logbook) if p["archive_id"] == str(archive_id or "")), None)


def _removed_ids(s: Any, site: str, logbook: str, lo: int, hi: int) -> set[str]:
    """The records of ``#lo`` to ``#hi`` a tombstone hides or removed (unit LD-6): those in a
    range ``retention.hidden_ranges`` names, and any whose content gold no longer projects."""
    from . import retention

    v = gold.view()
    covered = [(max(lo, a), min(hi, b)) for a, b, _t in retention.hidden_ranges(site, logbook)
               if a <= hi and b >= lo]  # fmt: skip
    gone = or_(v.c.content.is_(None), func.jsonb_typeof(v.c.content) == "null",
               *(v.c.seq.between(a, b) for a, b in covered))  # fmt: skip
    return {r[0] for r in s.execute(select(v.c.attestation_id).where(
        v.c.site_id == site, v.c.logbook == logbook, v.c.seq.between(lo, hi), gone))}  # fmt: skip


def _fold_key(removed: set[str]) -> str:
    """What a summary was folded from: every record of its period but these."""
    return "v1:" + hashlib.sha256("\n".join(sorted(removed)).encode("utf-8")).hexdigest()


# -- compaction: one summary per retired period ---------------------------------------------------


def _mine(idx: Any, site: str, logbook: str, space: Any) -> tuple[Any, ...]:
    return (idx.c.site_id == site, idx.c.logbook == logbook, idx.c.model == space.model,
            idx.c.dim == space.dim)  # fmt: skip


def compact(s: Any, site: str, logbook: str, *, space: Any, labels: dict[str, Any], roles: Any) -> None:
    """Bring one log's retired periods to their summaries, in the caller's transaction: reap the
    warm rows past their window, remove every other row of each period but its seal's, and fold
    the period's lexemes into the seal's chunk when what it was folded from changed."""
    idx = index._index()  # noqa: SLF001
    mine = _mine(idx, site, logbook, space)
    log = (idx.c.site_id == site, idx.c.logbook == logbook)  # every embedding space
    s.execute(delete(idx).where(*log, idx.c.warm_until.is_not(None), idx.c.warm_until <= _now()))
    for p in _periods(s, site, logbook, COMPACTED):
        in_period = (*log, idx.c.seq.between(p["from_seq"], p["last_seq"]))
        s.execute(delete(idx).where(*in_period, idx.c.warm_until.is_(None),
                                    idx.c.attestation_id != p["seal_id"]))  # fmt: skip
        removed = _removed_ids(s, site, logbook, p["from_seq"], p["last_seq"])
        if removed:
            s.execute(delete(idx).where(*in_period, idx.c.attestation_id.in_(sorted(removed))))
        if p["seal_id"] in removed:
            continue  # the seal itself was removed: there is no summary to serve
        key = _fold_key(removed)
        seal = (*mine, idx.c.attestation_id == p["seal_id"])
        folded = s.execute(select(idx.c.folded).where(*seal)).first()
        if folded is None or folded[0] == key:
            continue  # not indexed (yet), or already folded from exactly these records
        _fold(s, idx, p, seal, removed, labels=labels, roles=roles, space=space)
        s.execute(update(idx).where(*seal).values(folded=key))


def _fold(s: Any, idx: Any, p: dict[str, Any], seal: tuple[Any, ...], removed: set[str], *,
          labels: dict[str, Any], roles: Any, space: Any) -> None:  # fmt: skip
    """The seal's text-search column: its own words, then the lexemes of each record of the
    period that still has content, from gold, redacted as every chunk is. Positions are stripped:
    a summary answers whether its period holds a word, not where."""
    v = gold.view()

    def words(row: dict[str, Any]) -> str:
        return index.chunk_for(row, labels=labels, roles=roles, space=space).text

    seal_row = s.execute(select(v).where(v.c.attestation_id == p["seal_id"])).first()
    own = words(dict(seal_row._mapping)) if seal_row is not None else ""
    s.execute(update(idx).where(*seal).values(search_text=func.strip(func.to_tsvector(literal("simple"), own))))
    after = p["from_seq"] - 1
    while True:
        rows = [dict(r._mapping) for r in s.execute(select(v).where(
            v.c.site_id == p["site_id"], v.c.logbook == p["logbook"], v.c.seq > after,
            v.c.seq <= p["last_seq"]).order_by(v.c.seq).limit(BATCH))]  # fmt: skip
        if not rows:
            break
        after = int(rows[-1]["seq"])
        batch = "\n".join(words(r) for r in rows if r["attestation_id"] not in removed
                          and r["attestation_id"] != p["seal_id"] and r.get("content") is not None)  # fmt: skip
        if batch:
            s.execute(update(idx).where(*seal).values(search_text=func.strip(
                idx.c.search_text.op("||")(func.to_tsvector(literal("simple"), batch)))))


def reap() -> int:
    """Remove every warm row past its window, in every log; returns how many went."""
    with store.session_scope() as s:
        index.ensure(s)
        idx = index._index()  # noqa: SLF001
        n = s.execute(delete(idx).where(idx.c.warm_until.is_not(None), idx.c.warm_until <= _now()))
        s.commit()
        return int(n.rowcount or 0)


def live_clause(idx: Any) -> Any:
    """A row the search may rank: a hot row, a summary, or a warm row inside its window."""
    return or_(idx.c.warm_until.is_(None), idx.c.warm_until > _now())


# -- rehydrate: the period from its package, through the door, warm ------------------------------


def _admitted_for(p: dict[str, Any]) -> Any:
    from . import doors as log_doors

    a = log_doors.current()
    if a is None or a.target.site != p["site_id"] or a.target.scope != p["logbook"]:
        raise archives.ArchiveRefused(
            "an archived period is rehydrated through a door that admitted its log; nothing was read")
    return a


def rehydrate(archive_id: str) -> dict[str, Any]:
    """The records of an archived period, read from its package through the door that admitted
    its log (refused outside one), as chained; a removed record answers as removed. A retired
    period's records are indexed again as warm rows with the log's labels until the window ends.
    The read is noted for the door's read record (``what = rehydrate``, ``source`` the archive)."""
    from . import reads
    from . import registry as books

    a = archives._get(str(archive_id))  # noqa: SLF001
    p = {"archive_id": a.archive_id, "site_id": a.site_id, "logbook": a.logbook, "seal_id": a.seal_id,
         "from_seq": int(a.from_seq), "to_seq": int(a.to_seq), "last_seq": int(a.last_seq),
         "status": a.status, "package_digest": a.package_digest}  # fmt: skip
    admission = _admitted_for(p)
    if p["status"] not in archives.SERVING:
        raise archives.ArchiveRefused("this period is not served from an archive package (not yet archived, or erased under a tombstone)")
    records = archives.read_package(p["archive_id"])
    with store.session_scope() as s:
        index.ensure(s)
        removed = _removed_ids(s, p["site_id"], p["logbook"], p["from_seq"], p["last_seq"])
    out = [rec if rec["attestation_id"] not in removed else _without_content(rec) for rec in records]
    until = None
    if p["status"] == RETIRED:
        until = _warm(p, removed, labels=dict(admission.target.labels or {}),
                      roles=index.reader_roles(books.get(p["logbook"])))  # fmt: skip
    ids = [r["attestation_id"] for r in out]
    reads.note_access(what="rehydrate", site=p["site_id"], logbook=p["logbook"], ids=ids, count=len(ids))
    return {
        "period": {"archive_id": p["archive_id"], "from_seq": p["from_seq"], "to_seq": p["to_seq"],
                   "seal_seq": p["last_seq"]},
        "records": out, "warm_until": gold.iso(until),
        "source": f"archive:sha256:{p['package_digest']}",
    }  # fmt: skip


def _without_content(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in rec.items() if k not in ("content", "evidence")}
    out["removed"] = True
    return out


def _warm(p: dict[str, Any], removed: set[str], *, labels: dict[str, Any], roles: Any) -> datetime:
    """Index the period's records (not its seal, which is the summary) as warm rows."""
    reg = index.registry()
    space = reg.canonical
    until = _now() + warm_window()
    rows = [r for r in archives.gold_rows_from_package(p["archive_id"])
            if r["attestation_id"] not in removed and r["attestation_id"] != p["seal_id"]
            and r.get("content") is not None]  # fmt: skip
    for r in rows:
        r["occurred_at"], r["recorded_at"] = index._when(r["occurred_at"]), index._when(r.get("recorded_at"))  # noqa: SLF001
    chunks = [index.chunk_for(r, labels=labels, roles=roles, space=space) for r in rows]
    vectors = (reg.embed([c.text for c in chunks]) or [None] * len(chunks)) if chunks else []
    idx = index._index()  # noqa: SLF001
    with store.session_scope() as s:
        index.ensure(s)
        for chunk, vector in zip(chunks, vectors, strict=True):
            values = {**index._params(chunk, vector), "warm_until": until}  # noqa: SLF001
            up = pg_insert(idx).values(**values)
            s.execute(up.on_conflict_do_update(
                index_elements=["attestation_id", "model", "dim"],
                set_={"warm_until": up.excluded.warm_until, "embedding": up.excluded.embedding,
                      "search_text": up.excluded.search_text}))  # fmt: skip
        s.commit()
    return until


# -- what a result says about where it came from -------------------------------------------------


def mark(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each search result with ``archived`` (its record is in an archived period), and for those
    the ``period``, the ``source`` (the package) and ``summary`` (it is the seal standing for a
    retired period). Says where a result came from; never adds content."""
    known: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in results:
        pair = (r["site"], r["logbook"])
        if pair not in known:
            with store.session_scope() as s:
                known[pair] = _periods(s, *pair, (*archives.SERVING, TOMBSTONED))
        p = next((x for x in known[pair] if x["from_seq"] <= int(r["seq"]) <= x["last_seq"]), None)
        r["archived"] = p is not None
        r["summary"] = bool(p and p["status"] in COMPACTED and r["attestation_id"] == p["seal_id"])
        if p is not None:
            r["period"] = {"archive_id": p["archive_id"], "from_seq": p["from_seq"], "to_seq": p["to_seq"],
                           "seal_seq": p["last_seq"]}  # fmt: skip
            # A tombstoned period's package is gone: nothing came from it.
            r["source"] = f"archive:sha256:{p['package_digest']}" if p["status"] != TOMBSTONED else None
    return results


__all__ = [
    "INDEX_SOURCES",
    "WARM_ENV",
    "WARM_MAX",
    "compact",
    "live_clause",
    "mark",
    "period_of",
    "periods",
    "reap",
    "rehydrate",
    "warm_window",
]
