# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Time-series adoption as the first user of the switch runner (ADR-197, ADR-189 D5).

The copy (``adopt.run``) is the long ``prepare`` and keeps its own resumable
ledger. This module is the swap, the rollback and the drop of the old table, run
on :class:`axiom.infra.switchplan.SwitchPlan`:

- ``prepare``  refuses a partition and an unfinished copy, then catches the tail
  up outside any lock (:func:`adopt.catch_up_tail`);
- ``probe``    declares the gate: the copy is complete (``pre``), a read through
  the original name, the parent it hung from and every rebound view lands on the
  new table and none of the old (``in_commit``, :func:`adopt.check_reads_land_on_new`;
  and ``post``, the verify run again before the old is dropped);
- ``commit``   takes the exclusive lock under the plan's budget and runs
  :func:`adopt.swap_in_transaction`, with the gate inside the same transaction so
  a failure rolls the swap back for free;
- ``rollback`` swaps the names back (a swap that never committed has nothing to
  undo: its transaction rolled back);
- ``retire``   drops the old table. A ``reclaim`` retire: the operator's explicit
  ``--drop-old`` is the approval, recorded with their principal.

``adopt.swap``, ``adopt.rollback`` and ``adopt.drop_old`` keep their signatures
and messages; they call in here. A swap made before this existed has no runner
record, so its rollback and drop go straight to the direct code.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any

from axiom.infra.switchplan import (
    LockBudget,
    PgLedger,
    Probe,
    ProbeFailed,
    SingleFlight,
    SwitchPlan,
)

from . import adopt
from .contract import DEFAULT_LOCK_TIMEOUT, TimeseriesContract

KIND = "timeseries-adoption"
#: Rows per second the final pass under the lock is assumed to copy, for the
#: budget check made before the lock is taken. Conservative: a pass copies far
#: faster than this, and the catch-up passes leave fewer than a threshold of rows.
ASSUMED_ROWS_PER_S = 50_000.0
DEFAULT_PRINCIPAL = "@operator:local"


def principal_for(principal: str | None) -> str:
    return principal or os.environ.get("AXI_PRINCIPAL") or DEFAULT_PRINCIPAL


class AdoptionHooks:
    def __init__(self, conn, contract: TimeseriesContract, *, lock_timeout: str, tail_threshold_rows: int,
                 max_catchup_passes: int) -> None:
        self.conn, self.contract = conn, contract
        self.lock_timeout = lock_timeout
        self.tail_threshold_rows = tail_threshold_rows
        self.max_catchup_passes = max_catchup_passes

    # -- prepare: refuse, then catch up outside any lock ----------------------
    def prepare(self, ctx) -> None:
        c, conn = self.contract, self.conn
        adopt._ensure_ledger(conn)
        cur = conn.cursor()
        a = adopt._read_adoption(cur, c.qualified)
        if a is None or a["phase"] != "copying":
            raise adopt.AdoptionRefused(f"{c.qualified} is not being copied (phase {a['phase'] if a else 'none'})")
        adopt._refuse_partition(conn, c)
        p = adopt.progress(conn, c)
        if p.windows_done < p.windows_total:
            raise adopt.AdoptionRefused(
                f"{p.windows_total - p.windows_done} of {p.windows_total} window(s) are not copied yet; "
                "run the copy to the end first")
        src, new = adopt._q(c.schema, c.table), adopt._q(c.schema, c.table + adopt.NEW_SUFFIX)
        tail_from = p.last_window_end or adopt._aware(a["range_lo"]) or datetime(1970, 1, 1, tzinfo=adopt.UTC)
        cur.execute("SELECT %s::regclass::oid::int8, %s::regclass::oid::int8", (src, new))
        old_oid, new_oid = (int(v) for v in cur.fetchone())
        t0 = time.monotonic()
        caught = adopt.catch_up_tail(conn, c, tail_from, threshold_rows=self.tail_threshold_rows,
                                     max_passes=self.max_catchup_passes)
        ctx.state.update(
            tail_from=tail_from.isoformat(), final_from=caught["from"].isoformat(), old_oid=old_oid, new_oid=new_oid,
            parent=adopt.root_parent(cur, old_oid)[0], caught_rows=caught["rows"], caught_passes=caught["passes"],
            caught_last=caught["last"], catch_seconds=round(time.monotonic() - t0, 1), swapped=False)

    # -- the declared gate -----------------------------------------------------
    def _reads_land_on_new(self, ctx, cur, views: tuple[str, ...]) -> tuple[bool, str]:
        s = ctx.state
        try:
            lines = adopt.check_reads_land_on_new(
                cur, qualified=self.contract.qualified, old_oid=s["old_oid"], new_oid=s["new_oid"],
                parent=s.get("parent"), views=views)
        except adopt.SwapUnsafe as exc:
            return False, str(exc)
        return True, "; ".join(lines)

    def probe(self, ctx):
        c = self.contract
        views = lambda e: tuple(e.state.get("views", ()))  # noqa: E731
        return [
            Probe("copy-is-complete-and-verified", "callable", when="pre", fn=lambda e: self._copy_complete()),
            Probe("reads-through-the-original-name-land-on-the-new-table", "callable", when="in_commit",
                  proves="read_through", fn=lambda e: self._reads_land_on_new(ctx, e.cursor, views(e)),
                  spec={"name": c.qualified}),
            Probe("reads-through-the-original-name-still-land-on-the-new-table", "callable", when="post",
                  proves="read_through", fn=lambda e: self._reads_land_on_new(ctx, self.conn.cursor(), views(e)),
                  spec={"name": c.qualified}),
        ]

    def _copy_complete(self) -> tuple[bool, str]:
        p = adopt.progress(self.conn, self.contract)
        return p.windows_done >= p.windows_total, f"{p.windows_done}/{p.windows_total} windows"

    def tail_estimate(self, ctx) -> float:
        return float(ctx.state.get("caught_last", 0)) / ASSUMED_ROWS_PER_S

    # -- commit: the one bounded switch ---------------------------------------
    def commit(self, ctx, scope) -> str:
        import psycopg

        c, conn, s = self.contract, self.conn, ctx.state
        cur = conn.cursor()
        src = adopt._q(c.schema, c.table)
        tail_from = datetime.fromisoformat(s["tail_from"])
        final_from = datetime.fromisoformat(s["final_from"])
        last_error: Exception | None = None

        def after_rename(cur_, view_names: tuple[str, ...]) -> list[str]:
            s["views"] = list(view_names)
            try:
                results = scope.run_in_commit_probes(cur_)
            except ProbeFailed as exc:
                # the swap's own refusal, so a caller's handling of a swap that
                # would leave readers on the old table is unchanged
                raise adopt.SwapUnsafe(str(exc).split(": ", 1)[-1]) from exc
            return [r.detail for r in results]

        for attempt in range(adopt.SWAP_ATTEMPTS):
            try:
                t_wait = time.monotonic()
                with conn.transaction():
                    cur.execute(f"SET LOCAL lock_timeout = '{self.lock_timeout}'")
                    cur.execute(f"LOCK TABLE {src} IN ACCESS EXCLUSIVE MODE")
                    waited = time.monotonic() - t_wait
                    scope.bound_hold(cur)
                    with scope.holding():
                        steps, tail = adopt.swap_in_transaction(
                            cur, c, tail_from=tail_from, final_from=final_from, old_oid=s["old_oid"],
                            new_oid=s["new_oid"], parent=s.get("parent"), after_rename=after_rename)
                        s["swapped"] = True
                        s["lock_wait_seconds"] = round(waited, 3)
                        s["tail_rows_in_lock"] = tail
                        scope.record_in_tx(cur)
                held = scope.hold_s or 0.0
                measured = {"lock_held_seconds": round(held, 3), "lock_wait_seconds": round(waited, 3),
                            "tail_rows_in_lock": tail, "tail_rows_outside_lock": s["caught_rows"],
                            "catchup_passes": s["caught_passes"], "catchup_seconds": s["catch_seconds"]}
                cur.execute(
                    f"UPDATE {adopt.ADOPTIONS} SET notes = notes || jsonb_build_object('swap', %s::jsonb, 'switch_id', %s::text) "
                    "WHERE qualified = %s", (adopt._json(measured), ctx.record.id, c.qualified))
                steps.insert(0, (
                    f"{c.qualified} swapped: {s['caught_rows']:,} tail row(s) caught up outside any lock in "
                    f"{s['caught_passes']} pass(es) ({s['catch_seconds']:.1f} s), the last {tail:,} under a lock held "
                    f"{held:.2f} s (waited {waited:.2f} s to take it); the old table is {c.schema}.{c.table + adopt.OLD_SUFFIX}"))
                s["steps"] = steps
                return "; ".join(steps[:1])
            except psycopg.errors.LockNotAvailable as exc:
                last_error = exc
                s["swapped"] = False
                time.sleep(min(2 ** attempt, 30))
        raise adopt.AdoptionRefused(f"could not take the swap lock in {adopt.SWAP_ATTEMPTS} attempts: {last_error}")

    def rollback(self, ctx) -> str | None:
        if not ctx.state.get("swapped"):
            return None  # a swap that did not commit rolled back inside its transaction
        steps = adopt._rollback_direct(self.conn, self.contract, lock_timeout=self.lock_timeout)
        ctx.state["swapped"] = False
        ctx.state["rollback_steps"] = steps
        return "; ".join(steps)

    def retire(self, ctx) -> str:
        steps = adopt._drop_old_direct(self.conn, self.contract, lock_timeout=self.lock_timeout, reverify=ctx.state.get("reverify", True))
        ctx.state["retire_steps"] = steps
        return "; ".join(steps)


def _plan(conn, contract, hooks, *, who: str, record_id: str | None = None) -> SwitchPlan:
    return SwitchPlan(
        kind=KIND, subject=contract.qualified, hooks=hooks, ledger=PgLedger(conn, adopt.LEDGER_SCHEMA),
        lock=SingleFlight(adopt._lock_name(contract.qualified), conn=conn),
        budget=LockBudget(max_hold_s=2.0, attempts=adopt.SWAP_ATTEMPTS), change_kind="migration", data=True,
        retire="reclaim", owner=who, retire_after=timedelta(days=1), record_id=record_id,
        params={"cursor": conn.cursor()},
        rollback_command=f"axi data adopt-timeseries {contract.qualified} --rollback")


def swap_through_runner(conn, contract, *, lock_timeout=DEFAULT_LOCK_TIMEOUT, tail_threshold_rows, max_catchup_passes,
                        principal=None) -> list[str]:
    from axiom.infra.switchplan.errors import SwitchBusy

    hooks = AdoptionHooks(conn, contract, lock_timeout=lock_timeout, tail_threshold_rows=tail_threshold_rows,
                          max_catchup_passes=max_catchup_passes)
    plan = _plan(conn, contract, hooks, who=principal_for(principal))
    try:
        record = plan.run()
    except SwitchBusy as exc:
        raise adopt.AdoptionBusy(str(exc)) from exc
    return list(record.state.get("steps", []))


def _record_id(conn, contract) -> str | None:
    a = adopt._read_adoption(conn.cursor(), contract.qualified)
    return ((a or {}).get("notes") or {}).get("switch_id")


def _loaded_plan(conn, contract, *, lock_timeout, principal=None, reverify=True):
    rid = _record_id(conn, contract)
    if not rid:
        return None
    hooks = AdoptionHooks(conn, contract, lock_timeout=lock_timeout, tail_threshold_rows=adopt.TAIL_THRESHOLD_ROWS,
                          max_catchup_passes=adopt.MAX_CATCHUP_PASSES)
    plan = _plan(conn, contract, hooks, who=principal_for(principal), record_id=rid)
    plan.record.state["reverify"] = reverify
    return plan if plan.record.phase in ("committed", "soaked") else None


def rollback_through_runner(conn, contract, *, lock_timeout=DEFAULT_LOCK_TIMEOUT) -> list[str]:
    plan = _loaded_plan(conn, contract, lock_timeout=lock_timeout)
    if plan is None:
        return adopt._rollback_direct(conn, contract, lock_timeout=lock_timeout)
    plan.rollback("operator asked for a rollback")
    return list(plan.record.state.get("rollback_steps", []))


def drop_through_runner(conn, contract, *, lock_timeout=DEFAULT_LOCK_TIMEOUT, reverify=True, principal=None) -> list[str]:
    # The direct code owns the phase rule ("only after a verification passed"), so a
    # drop that must be refused is refused with its own message before the runner is asked.
    adopt._ensure_ledger(conn)
    a = adopt._read_adoption(conn.cursor(), contract.qualified)
    if a is None or a["phase"] != "verified" or a["verified_at"] is None:
        return adopt._drop_old_direct(conn, contract, lock_timeout=lock_timeout, reverify=reverify)
    plan = _loaded_plan(conn, contract, lock_timeout=lock_timeout, principal=principal, reverify=reverify)
    if plan is None:
        return adopt._drop_old_direct(conn, contract, lock_timeout=lock_timeout, reverify=reverify)
    who = principal_for(principal)
    plan.retire(approver=who, override_soak="operator-initiated --drop-old after a passing verify")
    return list(plan.record.state.get("retire_steps", []))


__all__: list[Any] = ["AdoptionHooks", "swap_through_runner", "rollback_through_runner", "drop_through_runner"]
