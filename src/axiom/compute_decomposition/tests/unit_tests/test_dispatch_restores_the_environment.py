# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dispatching a chunk must leave the parent's environment exactly as it was.

`SubprocessDispatcher` sets two variables for the CHILD: `PYTHONPATH`, so the
subprocess can import this repo, and `AXI_STATE_DIR`, so it resolves state under
the run's own directory. `PYTHONPATH` was always restored in a `finally`.
`AXI_STATE_DIR` was set once in `_make_runner` and never put back.

That asymmetry was a suite-wide fault. `AXI_STATE_DIR` is the highest-priority
input to `get_user_state_dir()`, so once any dispatch ran, every later caller in
the process resolved the user state dir to that run's temp directory. In a test
session that is every subsequent test, and it presented as unrelated suites
failing in different places on different runs:

    pytest tests/cli/test_role_cli.py                      -> 16 passed
    pytest src/axiom/compute_decomposition/ tests/cli/...  ->  7 failed

with `FileNotFoundError` on files the tests had just written, because they were
written somewhere else. The failures moved between `vault`, `diagnostics` and
`role` depending only on collection order, which is why it read as flake and
survived three separate pushes.

The assertion here is on the PARENT's environment after the call, which is the
quantity free to vary. Asserting the child received the variables would pass
just as well with the leak still in place.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom.compute_decomposition.runner import SubprocessDispatcher
from axiom.compute_decomposition.types import Chunk, Trait


def _chunk(cid: str) -> Chunk:
    return Chunk(
        chunk_id=cid,
        plan_id="plan-env-restore",
        sequence_index=0,
        trait=Trait.DETERMINISTIC,
        parameters={"start": 0, "end": 4},
    )


def _dispatch(d: SubprocessDispatcher, cid: str) -> None:
    """Run one chunk. Whether it SUCCEEDS is not what is under test.

    The environment must come back either way, so a failing dispatch exercises
    the `finally` path — which is precisely the path that was wrong.
    """
    try:
        d.dispatch_one(_chunk(cid))
    except Exception:  # noqa: BLE001 - the outcome is not the assertion
        pass


def _dispatcher(tmp_path: Path) -> SubprocessDispatcher:
    return SubprocessDispatcher(
        leaf_node_id="@test-leaf:local",
        principal="@test:local",
        state_dir=tmp_path / "axi_state",
        kernel_name="sum_of_squares",
        timeout_seconds=20.0,
    )


class TestTheParentEnvironmentSurvivesADispatch:
    def test_axi_state_dir_is_not_left_behind(self, tmp_path: Path) -> None:
        before = os.environ.get("AXI_STATE_DIR")
        d = _dispatcher(tmp_path)
        _dispatch(d, "c1")
        after = os.environ.get("AXI_STATE_DIR")

        assert after == before, (
            f"AXI_STATE_DIR escaped the dispatch: {before!r} -> {after!r}. "
            "Every later get_user_state_dir() in this process now resolves to "
            "the dispatch's directory."
        )

    def test_pythonpath_is_not_left_behind(self, tmp_path: Path) -> None:
        """The variable that was already correct — pinned so it stays that way."""
        before = os.environ.get("PYTHONPATH")
        d = _dispatcher(tmp_path)
        _dispatch(d, "c2")

        assert os.environ.get("PYTHONPATH") == before

    def test_no_variable_at_all_escapes_the_dispatch(self, tmp_path: Path) -> None:
        """Name the class, not the two instances of it.

        A third variable added for the child later would otherwise reintroduce
        exactly this bug with both tests above still green.
        """
        before = dict(os.environ)
        d = _dispatcher(tmp_path)
        _dispatch(d, "c3")
        after = dict(os.environ)

        changed = {
            k: (before.get(k), after.get(k))
            for k in set(before) | set(after)
            if before.get(k) != after.get(k)
        }
        assert not changed, f"dispatch mutated the parent environment: {changed}"
