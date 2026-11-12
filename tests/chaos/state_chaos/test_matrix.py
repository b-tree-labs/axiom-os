# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chaos matrix: every persisted artifact x every damage kind.

Each cell builds a populated state tree in a temp dir, damages ONE artifact,
runs the REAL reader in a subprocess pointed at that tree, and requires the
outcome be ``recovered`` or ``degraded-loudly`` -- never a crash, a hang, a
silent default over damaged user data, or a silent wrong answer.

Cells that currently fail are listed in ``known_findings.KNOWN`` and run as
strict xfails with their finding id.
"""

from __future__ import annotations

import pytest

from .artifacts import BY_ID
from .known_findings import KNOWN
from .matrix import cells, run_cell_tmp, verdict


def _params():
    for art_id, kind in cells():
        marks = []
        fid = KNOWN.get((art_id, kind))
        if fid:
            marks.append(pytest.mark.xfail(strict=True, reason=fid))
        yield pytest.param(art_id, kind, id=f"{art_id}-{kind}", marks=marks)


@pytest.mark.parametrize(("art_id", "kind"), list(_params()))
def test_damaged_state_never_crashes_or_lies(art_id, kind):
    art = BY_ID[art_id]
    cell = run_cell_tmp(art_id, kind)
    problem = verdict(art, cell)
    assert problem is None, (
        f"{art_id} x {kind}: {problem}\n"
        f"  reader outcome: {cell.klass} ({cell.why})\n"
        f"  probe: {cell.result.outcome} {cell.result.exc_type} {cell.result.exc_msg[:300]}\n"
        f"  log: {cell.result.log[:300]}"
    )


def test_every_known_finding_names_a_real_cell():
    real = set(cells())
    stale = [k for k in KNOWN if k not in real]
    assert not stale, f"known_findings lists cells that do not exist: {stale}"
