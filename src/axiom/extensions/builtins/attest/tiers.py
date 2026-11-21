# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tiered gold: one log's year moves to a cold table, and nothing that reads it changes (unit LD-7,
ADR-251 D7).

The gold view ``gold.log_records`` selects from ``attest_records``. A **partition** is one log's
records of one calendar year (by ``recorded_at``, UTC). The **hot tier** is ``attest_records``
itself; the **cold tier** is one table per moved partition, ``attest_records_cold_<year>_<hash>``,
created ``LIKE attest_records INCLUDING ALL`` in the declared cold tablespace and attached to
``attest_records`` by inheritance. A read of the chain, a read through the gold view, a
verification and a tombstone's erasure all name ``attest_records`` and so reach cold rows
unchanged, through the same doors and the same check: no reader names a tier, and this module is
the only place a cold table's name is built.

A move is a switch on the ADR-197 runner (kind ``attest.tier_move``):

- **plan** refuses, before anything is written, a partition inside its window (the newest
  ``AXIOM_ATTEST_HOT_YEARS`` calendar years stay hot, default 2), one holding a record that no
  verified archive covers (LD-5: a stored verify report for exactly that package digest), one
  whose rows are not in sequence order by time, one already cold, and a cold tablespace that does
  not exist.
- **prepare** builds the cold table beside the hot rows and copies them into it, in one
  transaction, and records the partition's gold digest and the digest list.
- the **pre probes** prove every row identical (every column) and every digest identical, and the
  cold table's gold projection's digest equal to the hot one's (LD-7-A1).
- **commit**, in one transaction bounded by the lock budget: take the log's chain lock, attach the
  cold table, delete the hot rows (the database allows each only for an identical row in the cold
  table this committing move names), and run the ``in_commit`` probe reading through the gold view
  (the partition's digest unchanged, no hot row left, every row from the cold table).
- the **post probes** check the same digest again and that the chain verifies across tiers.
- **rollback** after a commit moves the rows back (the database allows each cold row's delete
  only for an identical hot row), detaches the cold table and drops it once empty. Before a
  commit, the staged copy is not part of the chain and is dropped once the hot range is checked
  whole.
- **retire** is ``release``: nothing is destroyed (the cold table holds the only copy, which is
  the one the chain reads), so a passing soak closes the rollback window.

The database guards (migration 0020) refuse any change to a row while its partition moves, any
delete that is not one of the two above, any insert into a cold table but its own move's copy,
and a second copy of a moved record in the hot table.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from axiom.extensions.builtins.data_platform.tiers import STORAGE_TIER_MARK

from . import gold, store
from .db_models import AttestArchive, AttestRecord, AttestTierMove
from .service import AttestRefused

#: The switch kind on the ADR-197 runner.
KIND = "attest.tier_move"
#: Every cold table's name starts with this; only this module builds one.
PREFIX = "attest_records_cold_"
#: How many of the newest calendar years stay hot.
HOT_YEARS_ENV = "AXIOM_ATTEST_HOT_YEARS"
DEFAULT_HOT_YEARS = 2
#: The tablespace cold tables are created in; unset is the database's default tablespace.
TABLESPACE_ENV = "AXIOM_ATTEST_COLD_TABLESPACE"
#: A cold table's comment: this mark, then JSON naming its tier, window and rows, never a log or a
#: site (``data.tier_audit`` reads it from the catalogue).
COMMENT_MARK = STORAGE_TIER_MARK

#: Statuses of a move whose rows the cold table holds.
COLD = ("cold", "retired")
#: Archive statuses whose package may stand for the partition (with a stored verify report).
ARCHIVED = ("verified", "archived", "retired")

#: Seconds per row a commit is expected to hold its lock for (attach is constant; the delete is
#: per row, each checked against its copy), plus the attach.
_PER_ROW_S = 0.0002
_FIXED_S = 0.05


class TierRefused(AttestRefused):
    """A move that may not run, with nothing written."""


def _now() -> datetime:
    return datetime.now(UTC)


def hot_years() -> int:
    raw = os.environ.get(HOT_YEARS_ENV, "").strip()
    if not raw:
        return DEFAULT_HOT_YEARS
    try:
        n = int(raw)
    except ValueError as exc:
        raise TierRefused(f"{HOT_YEARS_ENV}={raw!r} is not a number of years") from exc
    if n < 1:
        raise TierRefused(f"{HOT_YEARS_ENV} must keep at least the current year hot (got {n})")
    return n


def cold_tablespace() -> str | None:
    name = os.environ.get(TABLESPACE_ENV, "").strip()
    if not name:
        return None
    gold.ident(name)
    return name


def window(year: int) -> dict[str, str]:
    """A year partition's window: ``[from, to)`` in UTC, as ISO 8601."""
    start = datetime(year, 1, 1, tzinfo=UTC)
    return {"from": start.isoformat(), "to": datetime(year + 1, 1, 1, tzinfo=UTC).isoformat()}


def past_window(year: int, now: datetime | None = None) -> bool:
    """True when ``year`` is older than the hot years ending with the one ``now`` is in."""
    now = now or _now()
    return year <= now.astimezone(UTC).year - hot_years()


def table_name(site: str, logbook: str, year: int) -> str:
    """The cold table of one log's year. The log and site are hashed so the name, which the
    catalogue shows anyone who can list tables, names neither."""
    h = hashlib.sha256(f"{site}\x00{logbook}".encode()).hexdigest()[:16]
    return gold.ident(f"{PREFIX}{int(year)}_{h}").strip('"')


def _owner() -> str:
    return gold._owner()  # noqa: SLF001 - the attest schema, validated by ident()


def _q(name: str) -> str:
    """``"<attest schema>"."<name>"``, both validated."""
    return f"{gold.ident(_owner())}.{gold.ident(name)}"


# -- what a partition is -------------------------------------------------------------------------


def _live(s: Any, site: str, logbook: str, year: int) -> AttestTierMove | None:
    return s.execute(select(AttestTierMove).where(
        AttestTierMove.site_id == site, AttestTierMove.logbook == logbook, AttestTierMove.year == year,
        AttestTierMove.status != "rolled_back")).scalar_one_or_none()  # fmt: skip


def _row(m: AttestTierMove) -> dict[str, Any]:
    return {
        "move_id": m.move_id, "site_id": m.site_id, "logbook": m.logbook, "year": int(m.year),
        "window": window(int(m.year)), "from_seq": int(m.from_seq), "to_seq": int(m.to_seq),
        "rows": int(m.rows), "table": m.table_name, "tablespace": m.tablespace or "pg_default",
        "status": m.status, "gold_digest": m.gold_digest, "archives": m.archives or [],
        "switch_id": m.switch_id, "created_by": m.created_by, "created_at": gold.iso(m.created_at),
        "committed_at": gold.iso(m.committed_at), "rolled_back_at": gold.iso(m.rolled_back_at),
    }  # fmt: skip


def get(move_id: str) -> dict[str, Any]:
    with store.session_scope() as s:
        m = s.get(AttestTierMove, move_id)
        if m is None:
            raise TierRefused(f"no tier move {move_id}")
        return _row(m)


def get_for(site: str, logbook: str, year: int) -> dict[str, Any] | None:
    """The live move of one log's year (not rolled back), or None."""
    with store.session_scope() as s:
        m = _live(s, site, logbook, int(year))
        return _row(m) if m else None


def listed(site: str, logbook: str) -> list[dict[str, Any]]:
    with store.session_scope() as s:
        rows = s.execute(select(AttestTierMove).where(
            AttestTierMove.site_id == site, AttestTierMove.logbook == logbook)
            .order_by(AttestTierMove.year, AttestTierMove.created_at)).scalars()  # fmt: skip
        return [_row(m) for m in rows]


def _year_of(col: Any) -> Any:
    return func.extract("year", func.timezone("UTC", col))


def _span(s: Any, site: str, logbook: str, year: int) -> tuple[int, int, int] | None:
    """``(from_seq, to_seq, rows)`` of one log's year, over every tier; None when it is empty."""
    start, end = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
    got = s.execute(select(func.min(AttestRecord.seq), func.max(AttestRecord.seq), func.count()).where(
        AttestRecord.site_id == site, AttestRecord.logbook == logbook,
        AttestRecord.recorded_at >= start, AttestRecord.recorded_at < end)).one()  # fmt: skip
    if not got[2]:
        return None
    lo, hi, n = int(got[0]), int(got[1]), int(got[2])
    if n != hi - lo + 1:
        raise TierRefused(f"{logbook}'s {year} records are not in sequence order by time "
                          f"(#{lo} to #{hi} holds {hi - lo + 1 - n} record(s) of another year); "
                          "a year partition must be one contiguous range")  # fmt: skip
    return lo, hi, n


def _covering(s: Any, site: str, logbook: str, lo: int, hi: int) -> list[dict[str, Any]]:
    """The verified archives that cover ``#lo`` to ``#hi``, or :class:`TierRefused` naming the first
    record none covers (LD-7-A4)."""
    from . import archives

    found = s.execute(select(AttestArchive).where(
        AttestArchive.site_id == site, AttestArchive.logbook == logbook,
        AttestArchive.status.in_(ARCHIVED)).order_by(AttestArchive.from_seq)).scalars()  # fmt: skip
    ranges = [a for a in found if archives.verified(a)]
    used: list[dict[str, Any]] = []
    seq = lo
    while seq <= hi:
        a = next((a for a in ranges if int(a.from_seq) <= seq <= int(a.last_seq)), None)
        if a is None:
            raise TierRefused(f"#{seq} of {logbook} is in no period with a verified archive; a partition "
                              "moves only after every period in it is archived and its package verified")  # fmt: skip
        used.append({"archive_id": a.archive_id, "package_digest": a.package_digest,
                     "from_seq": int(a.from_seq), "last_seq": int(a.last_seq)})  # fmt: skip
        seq = int(a.last_seq) + 1
    return used


def _tablespace_exists(s: Any, name: str) -> bool:
    return bool(s.execute(text("SELECT 1 FROM pg_tablespace WHERE spcname = :n"), {"n": name}).first())


def eligibility(site: str, logbook: str, year: int, *, now: datetime | None = None) -> dict[str, Any]:
    """What a move of one log's year would take, or :class:`TierRefused` saying why it may not run."""
    year = int(year)
    if not past_window(year, now):
        raise TierRefused(f"{year} is inside the hot window (the newest {hot_years()} year(s) stay hot); "
                          "a partition moves only once it is past its window")  # fmt: skip
    ts = cold_tablespace()
    with store.session_scope() as s:
        have = _live(s, site, logbook, year)
        if have is not None:
            raise TierRefused(f"{logbook}'s {year} already has a move ({have.status}, {have.move_id})")
        if ts and not _tablespace_exists(s, ts):
            raise TierRefused(f"the cold tablespace {ts} ({TABLESPACE_ENV}) does not exist here")
        span = _span(s, site, logbook, year)
        if span is None:
            raise TierRefused(f"{logbook} has no records in {year}")
        lo, hi, n = span
        used = _covering(s, site, logbook, lo, hi)
    return {"site_id": site, "logbook": logbook, "year": year, "from_seq": lo, "to_seq": hi, "rows": n,
            "table": table_name(site, logbook, year), "tablespace": ts, "archives": used}  # fmt: skip


# -- digests -------------------------------------------------------------------------------------


def _record_columns(conn: Any) -> list[str]:
    """``attest_records``' stored columns (the generated search vector is computed, never copied)."""
    rows = conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = :s "
        "AND table_name = 'attest_records' AND is_generated = 'NEVER' ORDER BY ordinal_position"),
        {"s": _owner()}).scalars().all()  # fmt: skip
    return [gold.ident(c).strip('"') for c in rows]


def _gold_rows(conn: Any, table: str, site: str, logbook: str, lo: int, hi: int, *, only: bool) -> list[dict]:
    """The gold projection (:data:`gold.PROJECTION`) of ``#lo`` to ``#hi`` of one log as one table
    holds them (``only``) or as the whole chain does."""
    scope = "ONLY " if only else ""
    # nosemgrep: avoid-sqlalchemy-text -- the table passed ident(); the projection is a constant; values are bound
    stmt = text(f"SELECT {gold.PROJECTION} FROM {scope}{_q(table)} WHERE site_id = :s AND logbook = :l "
                "AND seq BETWEEN :lo AND :hi ORDER BY seq")  # fmt: skip
    return [dict(r) for r in conn.execute(stmt, {"s": site, "l": logbook, "lo": lo, "hi": hi}).mappings()]


def _bodies(conn: Any, table: str, site: str, logbook: str, lo: int, hi: int) -> dict[str, str]:
    """Every row of ``#lo`` to ``#hi`` in exactly ``table``, by id: every column as JSON."""
    # nosemgrep: avoid-sqlalchemy-text -- the table passed ident(); values are bound
    stmt = text(f"SELECT r.attestation_id, (to_jsonb(r) - 'search_vector')::text AS body FROM ONLY {_q(table)} r "
                "WHERE r.site_id = :s AND r.logbook = :l AND r.seq BETWEEN :lo AND :hi")  # fmt: skip
    return {a: b for a, b in conn.execute(stmt, {"s": site, "l": logbook, "lo": lo, "hi": hi}).all()}


def _digests_sha(rows: list[dict]) -> str:
    return hashlib.sha256("\n".join(f"{r['seq']}:{r['digest']}" for r in rows).encode()).hexdigest()


def table_exists(table: str) -> bool:
    with store.session_scope() as s:
        return bool(s.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"{_owner()}.{table}"}).scalar())


def _attached(conn: Any, table: str) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM pg_inherits WHERE inhrelid = to_regclass(:c) AND inhparent = to_regclass(:p)"),
        {"c": f"{_owner()}.{table}", "p": f"{_owner()}.attest_records"}).first())  # fmt: skip


# -- the switch ------------------------------------------------------------------------------------


def _lit(value: str) -> str:
    """A SQL string literal for a CHECK constraint (DDL takes no bound parameter): only a value of
    the characters a site or log name may hold, quoted."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@/\-]{1,256}", value):
        raise TierRefused(f"refusing {value!r} in a partition's bounds")
    return "'" + value + "'"


class _Cursor:
    """What :class:`CommitScope` sets its budget through: one connection's driver SQL."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def execute(self, sql: str) -> None:
        self.conn.exec_driver_sql(sql)


class MoveHooks:
    """The four hooks of one move, ``retire`` and the commit's hold estimate."""

    def __init__(self, site: str, logbook: str, year: int, *, owner: str, now: datetime | None) -> None:
        self.site, self.logbook, self.year, self.owner, self.now = site, logbook, int(year), owner, now

    # prepare: build the cold table beside the hot rows ------------------------------------------

    def prepare(self, ctx: Any) -> None:
        st = ctx.state
        if st.get("move_id"):
            m = get(st["move_id"])
            if m["status"] != "preparing":
                return
        plan_ = eligibility(self.site, self.logbook, self.year, now=self.now)
        move_id, table, ts = str(uuid.uuid4()), plan_["table"], plan_["tablespace"]
        lo, hi = plan_["from_seq"], plan_["to_seq"]
        start, end = window(self.year)["from"], window(self.year)["to"]
        with store.session_scope() as s:
            conn = s.connection()
            s.add(AttestTierMove(
                move_id=move_id, site_id=self.site, logbook=self.logbook, year=self.year, from_seq=lo,
                to_seq=hi, rows=plan_["rows"], table_name=table, tablespace=ts, status="preparing",
                archives=plan_["archives"], switch_id=ctx.record.id, created_by=self.owner,
                created_at=datetime.now(UTC)))  # fmt: skip
            s.flush()
            space = f" TABLESPACE {gold.ident(ts)}" if ts else ""
            # nosemgrep: sqlalchemy-execute-raw-query -- DDL: names passed ident(); no value
            conn.exec_driver_sql(f"CREATE TABLE {_q(table)} (LIKE {_q('attest_records')} INCLUDING ALL){space}")
            # The partition's bounds, so the planner skips the table for any other log or year.
            bounds = (f"site_id = {_lit(self.site)} AND logbook = {_lit(self.logbook)} "
                      f"AND seq BETWEEN {int(lo)} AND {int(hi)} "
                      f"AND recorded_at >= {_lit(start.replace('+00:00', 'Z'))}::timestamptz "
                      f"AND recorded_at < {_lit(end.replace('+00:00', 'Z'))}::timestamptz")  # fmt: skip
            # nosemgrep: sqlalchemy-execute-raw-query -- DDL: names passed ident(); bounds are checked literals
            conn.exec_driver_sql(f"ALTER TABLE {_q(table)} ADD CONSTRAINT {gold.ident(table + '_bounds')} "
                                 f"CHECK ({bounds})")  # fmt: skip
            schema = gold.ident(_owner())
            for trig, when, fn in (("guard", "BEFORE UPDATE OR DELETE", "attest_records_guard()"),
                                   ("copy_only", "BEFORE INSERT", "attest_cold_copy_guard()")):  # fmt: skip
                # nosemgrep: sqlalchemy-execute-raw-query -- DDL: names passed ident(); no value
                conn.exec_driver_sql(f"CREATE TRIGGER {gold.ident(table + '_' + trig)} {when} ON {_q(table)} "
                                     f"FOR EACH ROW EXECUTE FUNCTION {schema}.{fn}")  # fmt: skip
            # nosemgrep: sqlalchemy-execute-raw-query -- DDL: names passed ident(); no value
            conn.exec_driver_sql(f"CREATE TRIGGER {gold.ident(table + '_no_truncate')} BEFORE TRUNCATE "
                                 f"ON {_q(table)} FOR EACH STATEMENT EXECUTE FUNCTION {schema}.attest_refuse_change()")
            comment = COMMENT_MARK + json.dumps({"tier": "cold", "of": "attest_records", "key": "recorded_at",
                                                 "window": window(self.year), "rows": plan_["rows"]},
                                                sort_keys=True)  # fmt: skip
            # nosemgrep: sqlalchemy-execute-raw-query -- DDL: the name passed ident(); the comment is escaped
            conn.exec_driver_sql(f"COMMENT ON TABLE {_q(table)} IS '" + comment.replace("'", "''") + "'")
            cols = ", ".join(gold.ident(c) for c in _record_columns(conn))
            # nosemgrep: avoid-sqlalchemy-text -- names passed ident(); values are bound
            copied = conn.execute(text(f"INSERT INTO {_q(table)} ({cols}) SELECT {cols} FROM ONLY "
                                       f"{_q('attest_records')} WHERE site_id = :s AND logbook = :l "
                                       "AND seq BETWEEN :lo AND :hi"),
                                  {"s": self.site, "l": self.logbook, "lo": lo, "hi": hi}).rowcount  # fmt: skip
            if copied != plan_["rows"]:
                raise TierRefused(f"copied {copied} row(s) of {plan_['rows']}; nothing was moved")
            hot = _gold_rows(conn, "attest_records", self.site, self.logbook, lo, hi, only=True)
            m = s.get(AttestTierMove, move_id)
            m.gold_digest, m.digests_sha256 = gold.partition_digest(hot), _digests_sha(hot)
            m.status = "prepared"
            s.commit()
        st.update(move_id=move_id, table=table, from_seq=lo, to_seq=hi, rows=plan_["rows"],
                  gold_digest=gold.partition_digest(hot), digests_sha256=_digests_sha(hot))  # fmt: skip

    def tail_estimate(self, ctx: Any) -> float:
        return _FIXED_S + _PER_ROW_S * int(ctx.state.get("rows") or 0)

    # probes ---------------------------------------------------------------------------------------

    def _rows_identical(self, ctx: Any) -> tuple[bool, str]:
        st = ctx.state
        m = get(st["move_id"])
        if m["status"] != "prepared":
            return False, f"the move is {m['status']}, not prepared"
        lo, hi = st["from_seq"], st["to_seq"]
        with store.session_scope() as s:
            conn = s.connection()
            hot = _bodies(conn, "attest_records", self.site, self.logbook, lo, hi)
            cold = _bodies(conn, st["table"], self.site, self.logbook, lo, hi)
        if len(hot) != st["rows"] or len(cold) != st["rows"]:
            return False, f"{len(hot)} hot and {len(cold)} cold row(s), {st['rows']} expected"
        differ = sorted(a for a in hot if hot[a] != cold.get(a))
        if differ:
            return False, f"{len(differ)} row(s) differ between the hot rows and the cold copy"
        return True, f"{len(hot)} row(s) identical, every column and digest"

    def _gold_identical(self, ctx: Any) -> tuple[bool, str]:
        st = ctx.state
        with store.session_scope() as s:
            conn = s.connection()
            hot = _gold_rows(conn, "attest_records", self.site, self.logbook, st["from_seq"], st["to_seq"], only=True)
            cold = _gold_rows(conn, st["table"], self.site, self.logbook, st["from_seq"], st["to_seq"], only=True)
        want = st["gold_digest"]
        got = (gold.partition_digest(hot), gold.partition_digest(cold), _digests_sha(cold))
        if got != (want, want, st["digests_sha256"]):
            return False, "the cold copy's gold digest or digest list is not the hot partition's"
        return True, f"gold digest {want[:12]} hot and cold"

    def _read_through(self, ctx: Any) -> tuple[bool, str]:
        st, conn = ctx.state, ctx.cursor
        if conn is None:
            return False, "no transaction to read through"
        lo, hi = st["from_seq"], st["to_seq"]
        rows = _gold_rows(conn, "attest_records", self.site, self.logbook, lo, hi, only=False)
        if gold.partition_digest(rows) != st["gold_digest"] or len(rows) != st["rows"]:
            return False, "the rows read through the gold view are not identical to the proved ones"
        if _bodies(conn, "attest_records", self.site, self.logbook, lo, hi):
            return False, "hot rows remain in the moved range"
        if len(_bodies(conn, st["table"], self.site, self.logbook, lo, hi)) != st["rows"]:
            return False, "the cold table does not hold every row"
        return True, f"{len(rows)} row(s) read through {gold.VIEW} from the cold table, digest unchanged"

    def _still_identical(self, ctx: Any) -> tuple[bool, str]:
        st = ctx.state
        m = get(st["move_id"])
        if m["status"] not in COLD:
            return False, f"the move is {m['status']}"
        rows = gold.partition(self.site, self.logbook, st["from_seq"], st["to_seq"])
        if gold.partition_digest(rows) != st["gold_digest"]:
            return False, "the partition read through the gold view changed"
        return True, "the partition reads through the gold view unchanged"

    def _chain_verifies(self, ctx: Any) -> tuple[bool, str]:
        from axiom.attest import verify

        rep = verify.chain(self.site, self.logbook)
        return (True, f"the chain verifies across tiers ({rep.checked} record(s))") if rep.ok else (
            False, f"the chain does not verify: {rep.reason} at #{rep.failed_seq}")  # fmt: skip

    def probe(self, ctx: Any) -> list[Any]:
        from axiom.infra.switchplan import Probe

        return [
            Probe(name="rows-identical", kind="callable", when="pre", fn=self._rows_identical),
            Probe(name="gold-digest-identical", kind="callable", when="pre", fn=self._gold_identical),
            Probe(name="gold-read-through", kind="callable", when="in_commit", proves="read_through",
                  fn=self._read_through),
            Probe(name="gold-partition-unchanged", kind="callable", when="post", fn=self._still_identical),
            Probe(name="chain-verifies", kind="callable", when="post", fn=self._chain_verifies),
        ]  # fmt: skip

    # commit and rollback ---------------------------------------------------------------------------

    def commit(self, ctx: Any, scope: Any) -> str:
        st = ctx.state
        table, lo, hi = st["table"], st["from_seq"], st["to_seq"]
        with store.session_scope() as s:
            conn = s.connection()
            scope.set_local(_Cursor(conn))
            with scope.holding():
                conn.execute(text("SELECT seq FROM attest_chain_heads WHERE site_id = :s AND logbook = :l "
                                  "FOR UPDATE"), {"s": self.site, "l": self.logbook})  # fmt: skip
                m = s.get(AttestTierMove, st["move_id"])
                if m.status != "prepared":
                    raise TierRefused(f"the move is {m.status}, not prepared")
                m.status = "committing"
                s.flush()
                # nosemgrep: sqlalchemy-execute-raw-query -- DDL: names passed ident(); no value
                conn.exec_driver_sql(f"ALTER TABLE {_q(table)} INHERIT {_q('attest_records')}")
                # nosemgrep: avoid-sqlalchemy-text -- the table passed ident(); values are bound
                gone = conn.execute(text(f"DELETE FROM ONLY {_q('attest_records')} WHERE site_id = :s "
                                         "AND logbook = :l AND seq BETWEEN :lo AND :hi"),
                                    {"s": self.site, "l": self.logbook, "lo": lo, "hi": hi}).rowcount  # fmt: skip
                if gone != st["rows"]:
                    raise TierRefused(f"{gone} hot row(s) removed of {st['rows']}; nothing was moved")
                scope.run_in_commit_probes(cursor=conn)
                m.status, m.committed_at = "cold", datetime.now(UTC)
                s.commit()
        return f"#{lo} to #{hi} of {self.logbook} ({self.year}) are read from the cold tier"

    def rollback(self, ctx: Any) -> str:
        st = ctx.state
        if not st.get("move_id"):
            return "nothing was built"
        table, lo, hi = st["table"], st["from_seq"], st["to_seq"]
        with store.session_scope() as s:
            conn = s.connection()
            m = s.get(AttestTierMove, st["move_id"])
            if m is None or m.status == "rolled_back":
                return "already rolled back"
            exists = bool(conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"),
                                       {"n": f"{_owner()}.{table}"}).scalar())  # fmt: skip
            conn.execute(text("SELECT seq FROM attest_chain_heads WHERE site_id = :s AND logbook = :l FOR UPDATE"),
                         {"s": self.site, "l": self.logbook})  # fmt: skip
            m.status = "rolling_back"
            s.flush()
            if exists and _attached(conn, table):
                cols = ", ".join(gold.ident(c) for c in _record_columns(conn))
                # nosemgrep: avoid-sqlalchemy-text -- names passed ident(); values are bound
                back = conn.execute(text(f"INSERT INTO {_q('attest_records')} ({cols}) SELECT {cols} FROM ONLY "
                                         f"{_q(table)} WHERE site_id = :s AND logbook = :l AND seq BETWEEN :lo AND :hi"),
                                    {"s": self.site, "l": self.logbook, "lo": lo, "hi": hi}).rowcount  # fmt: skip
                # nosemgrep: avoid-sqlalchemy-text -- the table passed ident(); values are bound
                gone = conn.execute(text(f"DELETE FROM ONLY {_q(table)} WHERE site_id = :s AND logbook = :l "
                                         "AND seq BETWEEN :lo AND :hi"),
                                    {"s": self.site, "l": self.logbook, "lo": lo, "hi": hi}).rowcount  # fmt: skip
                if back != gone:
                    raise TierRefused(f"{back} row(s) back in the hot tier but {gone} left the cold one")
                # nosemgrep: sqlalchemy-execute-raw-query -- DDL: names passed ident(); no value
                conn.exec_driver_sql(f"ALTER TABLE {_q(table)} NO INHERIT {_q('attest_records')}")
            hot = _bodies(conn, "attest_records", self.site, self.logbook, lo, hi)
            if len(hot) != int(m.rows):
                raise TierRefused(f"the hot tier holds {len(hot)} of {m.rows} row(s); the cold table is kept")
            if exists:
                # Never a drop in place of a move: the table is dropped only once it holds no row
                # (after a commit) or is a staged copy the chain never read and the hot range is whole.
                # nosemgrep: sqlalchemy-execute-raw-query -- DDL: the name passed ident(); no value
                conn.exec_driver_sql(f"DROP TABLE {_q(table)}")
            m.status, m.rolled_back_at = "rolled_back", datetime.now(UTC)
            s.commit()
        return f"#{lo} to #{hi} of {self.logbook} ({self.year}) are read from the hot tier"

    def retire(self, ctx: Any) -> str:
        with store.session_scope() as s:
            m = s.get(AttestTierMove, ctx.state["move_id"])
            if m.status == "cold":
                m.status = "retired"
                s.commit()
        return "the soak passed; the cold table holds the partition and the rollback window is closed"


def _switch_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "attest" / "tier-moves"


def plan(site: str, logbook: str, year: int, *, owner: str, now: datetime | None = None, budget: Any = None,
         soak: timedelta = timedelta(0), change_path: Path | None = None) -> Any:  # fmt: skip
    """The move of one log's year on the one runner. A new move is refused here, with nothing
    written, unless :func:`eligibility` holds."""
    from axiom.infra.switchplan import FileLedger, SwitchPlan

    year = int(year)
    have = get_for(site, logbook, year)
    if have is None:
        eligibility(site, logbook, year, now=now)
    p = SwitchPlan(kind=KIND, subject=f"{site}/{logbook}/{year}",
                   hooks=MoveHooks(site, logbook, year, owner=owner, now=now), ledger=FileLedger(_switch_dir()),
                   budget=budget, change_kind="migration", data=True, retire="release", owner=owner, soak=soak,
                   change_path=change_path, record_id=have["switch_id"] if have else None,
                   rollback_command=f"log tier-move rollback {site}/{logbook}/{year}")  # fmt: skip
    return p


def move(site: str, logbook: str, year: int, *, owner: str, now: datetime | None = None, budget: Any = None,
         change_path: Path | None = None) -> dict[str, Any]:  # fmt: skip
    """Prepare, prove and commit the move of one log's year. Refused, with nothing moved, when the
    partition may not move, a probe fails or the budget cannot be met."""
    p = plan(site, logbook, year, owner=owner, now=now, budget=budget, change_path=change_path)
    p.run()
    return get(p.record.state["move_id"])


def switch_record(move_id: str) -> Any:
    from axiom.infra.switchplan import FileLedger

    m = get(move_id)
    return FileLedger(_switch_dir()).load(m["switch_id"])


def drop_cold_refused(site: str, logbook: str, year: int) -> bool:
    """True when the database refuses removing a cold partition's rows outright (the guard holds):
    tried in a transaction that is always rolled back."""
    m = get_for(site, logbook, year)
    if m is None or m["status"] not in COLD:
        return False
    with store.session_scope() as s:
        try:
            # nosemgrep: avoid-sqlalchemy-text -- the table passed ident(); no value
            s.execute(text(f"DELETE FROM ONLY {_q(m['table'])}"))
        except Exception:  # noqa: BLE001 - the refusal is the answer
            s.rollback()
            return True
        s.rollback()
        return False


# -- the log's own report ------------------------------------------------------------------------


def partitions(site: str, logbook: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Each year of one log: its tier, window, range and rows, and whether it may move now and why
    not. Metadata only; the caller has already passed the log's door."""
    with store.session_scope() as s:
        years = s.execute(select(_year_of(AttestRecord.recorded_at).label("y"), func.min(AttestRecord.seq),
                                 func.max(AttestRecord.seq), func.count())
                          .where(AttestRecord.site_id == site, AttestRecord.logbook == logbook)
                          .group_by("y").order_by("y")).all()  # fmt: skip
        moves = {int(m.year): m for m in s.execute(select(AttestTierMove).where(
            AttestTierMove.site_id == site, AttestTierMove.logbook == logbook,
            AttestTierMove.status != "rolled_back")).scalars()}  # fmt: skip
        out = []
        for y, lo, hi, n in years:
            y = int(y)
            m = moves.get(y)
            row = {"year": y, "window": window(y), "from_seq": int(lo), "to_seq": int(hi), "rows": int(n),
                   "tier": "cold" if m is not None and m.status in COLD else "hot",
                   "move": {"move_id": m.move_id, "status": m.status} if m else None}  # fmt: skip
            out.append(row)
    for row in out:
        if row["tier"] == "cold" or row["move"]:
            row["eligible"], row["why_not"] = False, None if row["tier"] == "cold" else "a move is under way"
            continue
        try:
            eligibility(site, logbook, row["year"], now=now)
            row["eligible"], row["why_not"] = True, None
        except TierRefused as exc:
            row["eligible"], row["why_not"] = False, str(exc)
    return out


__all__ = [
    "COMMENT_MARK", "DEFAULT_HOT_YEARS", "HOT_YEARS_ENV", "KIND", "PREFIX", "TABLESPACE_ENV", "MoveHooks",
    "TierRefused", "cold_tablespace", "drop_cold_refused", "eligibility", "get", "get_for", "hot_years",
    "listed", "move", "partitions", "past_window", "plan", "switch_record", "table_exists", "table_name",
    "window",
]  # fmt: skip
