# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the restore drills.

The sandbox (temp state dir, HOME, file secrets backend, jsonl action ledger, no AXIOM_DB_URL, a
keychain that raises) is shared with ``tests/infra/backup_core`` and is asserted BEFORE anything
else runs; ``test_sandbox_guard.py`` there proves the guard fires.
"""

from __future__ import annotations

from collections import Counter

import pytest

from axiom.infra import backup_core as bc
from tests.infra.backup_core.conftest import (  # noqa: F401  (fixtures re-exported)
    CANARY_A,
    CANARY_B,
    PASSPHRASE,
    SandboxEscape,
    assert_sandboxed,
    fast_kdf,
    sandbox,
    state_dir,
)

from .harness import Harness, Judged
from .kinds import PgHarness, StateHarness, VaultHarness

#: (group, kind, scenario, outcome class) for every drill that ran; summarised at the end.
TALLY: list[tuple[str, str, str, str]] = []


def record(group: str, h: Harness, scenario: str, judged: Judged) -> Judged:
    TALLY.append((group, h.kind, scenario, judged.cls))
    return judged


@pytest.fixture(autouse=True)
def short_lock_waits(monkeypatch):
    """A drill that finds a lock held should learn so in half a second, not ten."""
    real = bc.backup_lock

    def quick(lockfile, **kw):
        return real(lockfile, **{**kw, "timeout": min(kw.get("timeout", 10.0), 0.4)})

    monkeypatch.setattr(bc, "backup_lock", quick)


@pytest.fixture
def make(sandbox, state_dir, fast_kdf, monkeypatch):  # noqa: F811
    def _make(kind: str) -> Harness:
        assert_sandboxed(sandbox)
        if kind == "state":
            return StateHarness(sandbox, state_dir)
        if kind == "vault":
            return VaultHarness(sandbox, state_dir)
        if kind == "pg":
            return PgHarness(sandbox, state_dir, monkeypatch)
        raise ValueError(kind)

    return _make


def pytest_terminal_summary(terminalreporter):
    if not TALLY:
        return
    w = terminalreporter.write_line
    w("")
    w("restore drills: outcome classes by backup kind")
    by = Counter((k, c) for _g, k, _s, c in TALLY)
    classes = sorted({c for _k, c in by})
    kinds = sorted({k for k, _c in by})
    w(f"  {'':10}" + "".join(f"{c:>26}" for c in classes) + f"{'total':>8}")
    for k in kinds:
        w(f"  {k:10}" + "".join(f"{by[(k, c)]:>26}" for c in classes) + f"{sum(by[(k, c)] for c in classes):>8}")
    w(f"  {'ALL':10}" + "".join(f"{sum(by[(k, c)] for k in kinds):>26}" for c in classes) + f"{len(TALLY):>8}")
