# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""guarded_act max_workers — concurrent per-candidate dispatch."""
from __future__ import annotations

import threading

from axiom.policy.agent_action_guard import AgentAction, guarded_act


def _act(n):
    return AgentAction(agent="t", op_class="t.ingest", name="t",
                       candidates=[f"c{i}" for i in range(n)] + ["bad"],
                       reversible=True)


def test_pool_runs_concurrently_and_collects(tmp_path):
    """Concurrency is asserted deterministically by a barrier, not by timing.

    Earlier versions timed the run (``elapsed < 0.4``). That reads
    thread-startup timing on a shared CI runner: if the pool spawns its second
    worker only after the first 0.05 s task has already returned, no overlap
    is *observed* even though the code is genuinely concurrent — and a loaded
    machine can push the whole run past any wall-clock bound.

    A ``Barrier`` sized to the candidate count removes the race. Every
    candidate must be inside ``do_one`` simultaneously to trip it; if the pool
    serialized the work, the first thread waits alone until the barrier's
    timeout and reports the break. Concurrency becomes a hard requirement with
    a clean failure, independent of how loaded the machine is.
    """
    n_good = 8
    parties = n_good + 1  # every good candidate + "bad"
    barrier = threading.Barrier(parties, timeout=5.0)
    threads: set[int] = set()
    lock = threading.Lock()
    broke = threading.Event()

    def do_one(c):
        with lock:
            threads.add(threading.get_ident())
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            broke.set()  # serialized: never all in flight at once
        return c != "bad"

    d = guarded_act(_act(n_good), do_one=do_one, state_dir=tmp_path,
                    volume_mode="off", max_workers=parties)

    assert not broke.is_set(), "candidates did not run concurrently (barrier timed out)"
    assert len(threads) == parties, f"{len(threads)} workers for {parties} candidates"
    assert d.proceed
    assert len(d.completed) == n_good and d.refused == ["bad"]


def test_sequential_default_preserves_order(tmp_path):
    d = guarded_act(_act(5), do_one=lambda c: c != "bad", state_dir=tmp_path,
                    volume_mode="off")
    assert d.completed == [f"c{i}" for i in range(5)]
    assert d.refused == ["bad"]


def test_pool_respects_pause_gate(tmp_path):
    # pause sentinel short-circuits BEFORE any concurrent dispatch
    from axiom.policy.agent_action_guard import pause_action
    pause_action(state_dir=tmp_path, agent="t", scope="all", by="x", reason="y")
    calls = []
    d = guarded_act(_act(4), do_one=lambda c: calls.append(c) or True,
                    state_dir=tmp_path, volume_mode="off", max_workers=4)
    assert not d.proceed and not calls   # paused: nothing ran
