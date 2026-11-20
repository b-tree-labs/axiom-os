# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY proposes; it does not act. And a lane in use is not reclaimable."""

from __future__ import annotations


from axiom.extensions.builtins.hygiene import lane_reclaim
from axiom.extensions.builtins.lane.registry import Lane


def _lane(name, root, branch="feat/x", database="axiom_lane_x", dsn_var="AXIOM_DB_URL"):
    return Lane(name=name, front=8800, api=8801, database=database,
                branch=branch, root=str(root), dsn_var=dsn_var)


def test_a_lane_whose_checkout_is_gone_is_reclaimable(tmp_path):
    items = lane_reclaim.assess({"x": _lane("x", tmp_path / "vanished")},
                                workspace=tmp_path, venvs=[])

    assert len(items) == 1 and items[0].actionable
    assert "is gone" in " ".join(items[0].why)


def test_a_landed_branch_is_reclaimable(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    items = lane_reclaim.assess({"x": _lane("x", root)}, workspace=tmp_path,
                                venvs=[], branch_landed=lambda _b: True)

    assert items[0].actionable and "has landed" in " ".join(items[0].why)


def test_a_live_branch_is_left_alone(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    items = lane_reclaim.assess({"x": _lane("x", root)}, workspace=tmp_path,
                                venvs=[], branch_landed=lambda _b: False)

    assert items == []


def test_not_knowing_whether_it_landed_blocks_rather_than_proceeds(tmp_path):
    """The direction where being wrong costs something."""
    root = tmp_path / "wt"
    root.mkdir()
    items = lane_reclaim.assess({"x": _lane("x", root)}, workspace=tmp_path,
                                venvs=[], branch_landed=lambda _b: None)

    assert items and not items[0].actionable
    assert "cannot tell" in " ".join(items[0].blocked_by)


def test_a_bound_checkout_blocks_reclamation_however_landed(tmp_path):
    """F2 again: landed, clean, and something is importing from it."""
    root = tmp_path / "wt"
    root.mkdir()
    site = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "__editable__.pkg.pth").write_text(f"{root}\n")

    items = lane_reclaim.assess({"x": _lane("x", root)}, workspace=tmp_path,
                                venvs=[tmp_path / ".venv"], branch_landed=lambda _b: True)

    assert not items[0].actionable
    assert "imports" in " ".join(items[0].blocked_by)
    assert "git cannot see" in " ".join(items[0].blocked_by)


def test_it_proposes_and_never_executes(tmp_path):
    items = lane_reclaim.assess({"x": _lane("x", tmp_path / "gone")},
                                workspace=tmp_path, venvs=[])
    proposed = items[0].proposed

    assert proposed[0].startswith("axi lane release ")
    assert any(c.startswith("dropdb ") and "irreversible" in c for c in proposed)


def test_an_unisolated_lane_proposes_no_database_drop(tmp_path):
    lane = _lane("x", tmp_path / "gone", database="", dsn_var="unmanaged")
    items = lane_reclaim.assess({"x": lane}, workspace=tmp_path, venvs=[])

    assert not any("dropdb" in c for c in items[0].proposed)


def test_the_render_counts_what_is_blocked(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    site = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "__editable__.pkg.pth").write_text(f"{root}\n")
    items = lane_reclaim.assess({"x": _lane("x", root)}, workspace=tmp_path,
                                venvs=[tmp_path / ".venv"], branch_landed=lambda _b: True)

    assert "1 blocked" in lane_reclaim.render(items)


def test_nothing_to_reclaim_says_so(tmp_path):
    assert lane_reclaim.render([]) == "nothing to reclaim"
