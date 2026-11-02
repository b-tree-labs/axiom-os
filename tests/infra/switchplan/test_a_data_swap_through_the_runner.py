# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-197 on a real Postgres (container ``sw-<hex>``): a table swap through the runner.

The 2026-10-20 failure, reproduced: the target is a partition of a partitioned
table, so the swapped-in table is never attached to the parent and readers stay on
the old one. The in-commit read-through probe must catch it and roll the commit
back, with a writer running through the whole thing losing nothing.

The domain code here (``SwapHooks``) is the kind the table adoption becomes in P1:
bulk and catch-up copies outside the lock, one bounded transaction, the gate inside
it, the ledger row written in the same transaction.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from axiom.infra import change_intent
from axiom.infra.switchplan.errors import BudgetUnmeetable, GateNotPassed, ProbeFailed, SwitchBusy
from axiom.infra.switchplan.ledger import PgLedger
from axiom.infra.switchplan.lock import SingleFlight
from axiom.infra.switchplan.plan import SwitchPlan
from axiom.infra.switchplan.probes import Probe

S = "sw_t"
OWNER = "@ben.booth:axiom"


class Writer(threading.Thread):
    """Inserts a row every ~2 ms through the NAME the readers use, on its own connection."""

    def __init__(self, dsn, through):
        super().__init__(daemon=True)
        self.dsn, self.through = dsn, through
        self.acked: list[int] = []
        self.errors: list[str] = []
        self.max_wait = 0.0
        self._halt = threading.Event()
        self._n = 10_000_000

    def run(self):
        with psycopg.connect(self.dsn, autocommit=True, prepare_threshold=None) as c:
            while not self._halt.is_set():
                self._n += 1
                t0 = time.monotonic()
                try:
                    c.execute(f"INSERT INTO {self.through} (id, v) VALUES (%s, 'w')", (self._n,))
                    self.acked.append(self._n)
                except Exception as exc:  # noqa: BLE001 - counted, the bar is zero
                    self.errors.append(str(exc))
                self.max_wait = max(self.max_wait, time.monotonic() - t0)
                time.sleep(0.002)

    def finish(self):
        self._halt.set()
        self.join(timeout=10)


class SwapHooks:
    def __init__(self, dsn, *, target, through, tail_rows_per_s=50_000.0):
        self.dsn, self.target, self.through = dsn, target, through
        self.schema, self.table = target.split(".")
        self.new = f"{target}__new"
        self.old = f"{target}__old"
        self.rate = tail_rows_per_s
        self.conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)

    # -- prepare: expand outside any lock, resumable, repeated as catch-up
    def prepare(self, ctx):
        c = self.conn
        if c.execute("SELECT to_regclass(%s)", (self.new,)).fetchone()[0] is None:
            c.execute(f"CREATE TABLE {self.new} (LIKE {self.target} INCLUDING ALL)")
        ctx.state["new_oid"] = c.execute("SELECT %s::regclass::oid::int8", (self.new,)).fetchone()[0]
        ctx.state["old_oid"] = c.execute("SELECT %s::regclass::oid::int8", (self.target,)).fetchone()[0]
        upto = ctx.state.get("copied_upto", 0)
        n = c.execute(
            f"WITH moved AS (INSERT INTO {self.new} SELECT * FROM {self.target} WHERE id > %s RETURNING id) "
            "SELECT count(*), coalesce(max(id), %s) FROM moved", (upto, upto)).fetchone()
        ctx.state["copied_upto"] = max(upto, int(n[1]))

    def probe(self, ctx):
        s = ctx.state
        return [
            Probe("new-table-has-the-bulk", "callable", when="pre",
                  fn=lambda e: (c := self.conn.execute(f"SELECT count(*) FROM {self.new}").fetchone()[0]) > 0 and (True, f"{c} rows")),
            Probe("readers-land-on-the-new-table", "relation_read", when="in_commit",
                  spec={"name": self.through, "must_scan": [s.get("new_oid", 0)], "must_not_scan": [s.get("old_oid", 0)]}),
            Probe("reads-through-the-original-name-still-land-on-the-new-table", "relation_read", when="post",
                  spec={"name": self.through, "must_scan": [s.get("new_oid", 0)], "must_not_scan": [s.get("old_oid", 0)]}),
        ]

    def tail_estimate(self, ctx):
        rows = self.conn.execute(f"SELECT count(*) FROM {self.target} WHERE id > %s", (ctx.state["copied_upto"],)).fetchone()[0]
        return rows / self.rate

    def commit(self, ctx, scope):
        upto = ctx.state["copied_upto"]
        with psycopg.connect(self.dsn, autocommit=True, prepare_threshold=None) as c, scope.holding():
            with c.transaction():
                cur = c.cursor()
                scope.set_local(cur)
                cur.execute(f"LOCK TABLE {self.target} IN ACCESS EXCLUSIVE MODE")
                cur.execute(f"INSERT INTO {self.new} SELECT * FROM {self.target} WHERE id > %s", (upto,))
                cur.execute(f'ALTER TABLE {self.target} RENAME TO "{self.table}__old"')
                cur.execute(f'ALTER TABLE {self.new} RENAME TO "{self.table}"')
                scope.run_in_commit_probes(cur)  # raises ProbeFailed -> the transaction rolls back
                ctx.state["swapped"] = True
                scope.record_in_tx(cur)
        return f"{self.target} swapped"

    def rollback(self, ctx):
        c = self.conn
        if not ctx.state.get("swapped"):
            c.execute(f"DROP TABLE IF EXISTS {self.new}")
            return "never swapped; the copy was discarded"
        upto = ctx.state["copied_upto"]
        with c.transaction():
            c.execute(f"LOCK TABLE {self.target} IN ACCESS EXCLUSIVE MODE")
            c.execute(f"INSERT INTO {self.old} SELECT * FROM {self.target} WHERE id > %s ON CONFLICT DO NOTHING", (upto,))
            c.execute(f'ALTER TABLE {self.target} RENAME TO "{self.table}__rolledback"')
            c.execute(f'ALTER TABLE {self.old} RENAME TO "{self.table}"')
            c.execute(f"DROP TABLE {self.target}__rolledback")
        ctx.state["swapped"] = False
        return "swapped back"

    def retire(self, ctx):
        self.conn.execute(f"DROP TABLE {self.old}")
        return f"{self.old} dropped"


@pytest.fixture
def plain(sw_db, sw_dsn):
    sw_db.execute(f"CREATE TABLE {S}.t (id bigint PRIMARY KEY, v text)")
    sw_db.execute(f"INSERT INTO {S}.t SELECT g, 'seed' FROM generate_series(1, 20000) g")
    return sw_db


def _plan(sw_db, sw_dsn, hooks, tmp_path, **kw):
    kw.setdefault("owner", OWNER)
    kw.setdefault("lock", SingleFlight(f"{hooks.target}", conn=sw_db))
    kw.setdefault("data", True)
    kw.setdefault("params", {"cursor": sw_db.cursor()})
    return SwitchPlan(kind="table-swap", subject=hooks.target, hooks=hooks, ledger=PgLedger(sw_db, S), change_path=tmp_path / "c.jsonl", **kw)


def test_a_swap_under_a_writer_loses_nothing_holds_the_lock_briefly_and_reads_land_on_the_new_table(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    p = _plan(plain, sw_dsn, h, tmp_path)
    w = Writer(sw_dsn, f"{S}.t")
    w.start()
    time.sleep(0.2)
    p.prepare()
    p.prove()
    p.commit()
    time.sleep(0.2)
    w.finish()
    assert w.errors == [] and len(w.acked) > 20
    missing = plain.execute(f"SELECT count(*) FROM unnest(%s::bigint[]) a WHERE a NOT IN (SELECT id FROM {S}.t)", (w.acked,)).fetchone()[0]
    assert missing == 0
    assert plain.execute(f"SELECT count(*) FROM {S}.t").fetchone()[0] >= 20000 + len(w.acked)
    hold = p.record.holds[-1]
    assert hold["breach"] is False and hold["hold_s"] <= 2.0
    assert w.max_wait < 2.5
    # the ledger row was written by the commit's own transaction
    assert PgLedger(plain, S).load(p.record.id).phase == "committed"
    assert [c.outcome for c in change_intent.changes(path=tmp_path / "c.jsonl")] == ["switched"]


def test_the_partition_swap_of_2026_10_20_is_caught_inside_the_commit_and_rolled_back(plain, sw_dsn, tmp_path):
    plain.execute(f"CREATE TABLE {S}.p (id bigint, v text) PARTITION BY RANGE (id)")
    plain.execute(f"CREATE TABLE {S}.p_d PARTITION OF {S}.p DEFAULT")
    plain.execute(f"INSERT INTO {S}.p SELECT g, 'seed' FROM generate_series(1, 5000) g")
    # the swapped-in table can never be a partition of p, so readers through p stay on the old one
    h = SwapHooks(sw_dsn, target=f"{S}.p_d", through=f"{S}.p")
    p = _plan(plain, sw_dsn, h, tmp_path)
    w = Writer(sw_dsn, f"{S}.p")
    w.start()
    time.sleep(0.2)
    p.prepare()
    p.prove()
    with pytest.raises(ProbeFailed, match="readers-land-on-the-new-table.*which it must not"):
        p.commit()
    time.sleep(0.2)
    w.finish()
    assert p.record.phase == "rolled_back"
    assert w.errors == []
    # nothing changed: the partition is still the partition, the copy is gone, every written row is there
    assert plain.execute("SELECT to_regclass(%s)", (f"{S}.p_d__old",)).fetchone()[0] is None
    assert plain.execute("SELECT to_regclass(%s)", (f"{S}.p_d__new",)).fetchone()[0] is None
    assert plain.execute(f"SELECT count(*) FROM {S}.p_d").fetchone()[0] > 0
    missing = plain.execute(f"SELECT count(*) FROM unnest(%s::bigint[]) a WHERE a NOT IN (SELECT id FROM {S}.p)", (w.acked,)).fetchone()[0]
    assert missing == 0
    assert [c.outcome for c in change_intent.changes(path=tmp_path / "c.jsonl")] == ["failed"]
    # and the ledger never says committed
    assert PgLedger(plain, S).load(p.record.id).phase == "rolled_back"


def test_an_oversize_tail_is_refused_before_the_lock_is_taken_then_catch_up_fits(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t", tail_rows_per_s=5_000.0)
    p = _plan(plain, sw_dsn, h, tmp_path)
    p.prepare()
    p.prove()
    plain.execute(f"INSERT INTO {S}.t SELECT g, 'late' FROM generate_series(100000, 130000) g")  # 30k rows -> ~6 s at 5k/s
    with pytest.raises(BudgetUnmeetable, match="budget is 2 s"):
        p.commit()
    assert plain.execute("SELECT to_regclass(%s)", (f"{S}.t__old",)).fetchone()[0] is None  # no lock taken, nothing renamed
    p.prepare()  # a catch-up pass, outside the lock
    p.commit()
    assert p.record.holds[-1]["hold_s"] <= 2.0
    assert plain.execute(f"SELECT count(*) FROM {S}.t WHERE v = 'late'").fetchone()[0] == 30001


def test_a_swap_with_no_read_through_probe_cannot_be_declared(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    h.probe = lambda ctx: [Probe("rows", "callable", fn=lambda e: True)]  # type: ignore[method-assign]
    p = _plan(plain, sw_dsn, h, tmp_path)
    with pytest.raises(GateNotPassed, match="relation_read"):
        p.prove()


def test_rollback_after_commit_carries_back_what_the_writer_wrote_since(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    p = _plan(plain, sw_dsn, h, tmp_path)
    p.run()
    plain.execute(f"INSERT INTO {S}.t VALUES (999999, 'after-swap')")
    p.rollback("operator asked")
    assert p.record.phase == "rolled_back"
    assert plain.execute(f"SELECT v FROM {S}.t WHERE id = 999999").fetchone()[0] == "after-swap"
    assert plain.execute(f"SELECT count(*) FROM {S}.t").fetchone()[0] == 20001
    assert plain.execute("SELECT to_regclass(%s)", (f"{S}.t__old",)).fetchone()[0] is None


def test_retire_drops_the_old_only_with_an_approver_after_the_soak_and_a_verify(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    p = _plan(plain, sw_dsn, h, tmp_path)
    p.run()
    assert p.record.retire_task["owner"] == OWNER
    assert plain.execute("SELECT to_regclass(%s)", (f"{S}.t__old",)).fetchone()[0] is not None
    assert p.soak_check() == "soaked"
    p.retire(approver=OWNER)
    assert plain.execute("SELECT to_regclass(%s)", (f"{S}.t__old",)).fetchone()[0] is None
    assert p.record.phase == "retired"


def test_a_second_runner_on_the_same_table_is_refused_by_the_advisory_lock(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    with psycopg.connect(sw_dsn, autocommit=True) as other:
        holder = SingleFlight(f"{S}.t", conn=other)
        assert holder.acquire()
        p = _plan(plain, sw_dsn, h, tmp_path)
        with pytest.raises(SwitchBusy):
            p.prepare()
        holder.release()
    p.prepare()


def test_a_killed_run_resumes_from_the_ledger_and_finishes(plain, sw_dsn, tmp_path):
    h = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    p = _plan(plain, sw_dsn, h, tmp_path)
    p.prepare()
    p.prove()
    rid = p.record.id
    # the process "dies": a new plan object, new hooks and connections, same ledger and record id
    h2 = SwapHooks(sw_dsn, target=f"{S}.t", through=f"{S}.t")
    p2 = SwitchPlan(kind="table-swap", subject=h2.target, hooks=h2, ledger=PgLedger(plain, S), owner=OWNER, data=True,
                    change_path=tmp_path / "c.jsonl", record_id=rid, lock=SingleFlight(h2.target, conn=plain))
    assert p2.record.phase == "proved" and p2.record.state["copied_upto"] == 20000
    p2.commit()
    assert plain.execute(f"SELECT count(*) FROM {S}.t").fetchone()[0] == 20000
