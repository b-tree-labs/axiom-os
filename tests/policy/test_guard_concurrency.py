# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""guarded_act max_workers — concurrent per-candidate dispatch."""
from __future__ import annotations

import threading
import time

from axiom.policy.agent_action_guard import AgentAction, guarded_act


def _act(n):
    return AgentAction(agent="t", op_class="t.ingest", name="t",
                       candidates=[f"c{i}" for i in range(n)] + ["bad"],
                       reversible=True)


def test_pool_runs_concurrently_and_collects(tmp_path):
    threads = set()
    lock = threading.Lock()

    def do_one(c):
        with lock:
            threads.add(threading.get_ident())
        time.sleep(0.05)
        return c != "bad"

    t0 = time.time()
    d = guarded_act(_act(8), do_one=do_one, state_dir=tmp_path,
                    volume_mode="off", max_workers=8)
    elapsed = time.time() - t0
    assert d.proceed
    assert len(d.completed) == 8 and d.refused == ["bad"]
    assert len(threads) > 1           # actually parallel
    assert elapsed < 0.4              # 9 items would be ~0.45s sequential


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
