# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A ``logbook_inputs`` file is loaded once, whole, however many threads discover at once (#1799).

A node discovers its logs from more than one thread at start-up. The loader used to put the module
in ``sys.modules`` before running it, so a second thread could take the half-run module and fail
with "has no attribute 'register'", and the failure took every log's discovery with it.
"""

from __future__ import annotations

import sys
import threading

from axiom.extensions.builtins.attest import registry


def test_two_threads_load_one_inputs_file_and_both_see_its_function(tmp_path, monkeypatch):
    path = tmp_path / "slow_inputs.py"
    path.write_text(
        "import time\ncalls = []\ntime.sleep(0.3)\n\ndef register(inputs):\n    calls.append(inputs)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_inputs_ran", set())
    monkeypatch.delitem(sys.modules, "axiom_logbook_inputs.slow_ext", raising=False)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            registry._run_inputs("slow_ext", "slow_inputs:register", path)
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(sys.modules["axiom_logbook_inputs.slow_ext"].calls) == 1


def test_a_file_that_fails_to_load_leaves_no_half_module(tmp_path, monkeypatch):
    import pytest

    path = tmp_path / "bad_inputs.py"
    path.write_text("raise ImportError('a missing dependency')\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_inputs_ran", set())
    monkeypatch.delitem(sys.modules, "axiom_logbook_inputs.bad_ext", raising=False)
    with pytest.raises(registry.LogbookError, match="missing dependency"):
        registry._run_inputs("bad_ext", "bad_inputs:register", path)
    assert "axiom_logbook_inputs.bad_ext" not in sys.modules
