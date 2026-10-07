# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A pinned serving tree can be stale, and staleness is quieter than absence.

Three worktrees were deleted under a running node and it went on serving
from directories that no longer existed until somebody restarted it. The
fix was to pin: `axiom-service`, `nos-service`, `appkit-service`, each a
detached worktree so a serving node does not move when somebody checks out
a branch in the tree they develop in.

Pinning does not stop the tree going stale, and a stale pin says nothing at
all: the node serves, every probe is green, and it is serving whatever was
on main when somebody last remembered. `axiom-service` was nine commits
behind when this test was written.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.lane.doctor import DRIFT, check_pins
from axiom.extensions.builtins.lane.registry import Lane

ROOT = Path("/w")


def lane(trees):
    return {"standing": Lane(name="standing", front=8770, api=8771, trees=trees)}


def state_of(table):
    """`state(path) -> (pinned, behind)`, injected so this needs no git."""
    def _state(path: Path):
        return table.get(path.name)
    return _state


def test_a_pinned_tree_behind_main_is_reported(tmp_path):
    (tmp_path / "axiom-service").mkdir()
    findings = check_pins(
        lane(["axiom-service"]), tmp_path, state=state_of({"axiom-service": (True, 9)})
    )

    assert len(findings) == 1
    assert findings[0].level == DRIFT
    assert "9 commit(s) behind" in findings[0].detail


def test_the_fix_is_checkout_and_says_why_it_is_not_pull(tmp_path):
    """`git pull` fails on a detached HEAD, which is what a pin is. Offering
    it would send somebody to an error message instead of a fix."""
    (tmp_path / "axiom-service").mkdir()
    [f] = check_pins(lane(["axiom-service"]), tmp_path, state=state_of({"axiom-service": (True, 3)}))

    assert "checkout origin/main" in f.fix
    assert "pull fails on a detached HEAD" in f.fix


def test_a_pinned_tree_that_is_current_is_not_a_finding(tmp_path):
    (tmp_path / "nos-service").mkdir()
    assert check_pins(lane(["nos-service"]), tmp_path, state=state_of({"nos-service": (True, 0)})) == []


def test_a_tree_on_a_branch_is_not_a_pin_and_is_left_alone(tmp_path):
    """A development worktree is SUPPOSED to be behind main. Reporting it
    would make the check noise, and noise is how a real finding gets
    scrolled past."""
    (tmp_path / "axiom-appkit").mkdir()
    assert check_pins(lane(["axiom-appkit"]), tmp_path, state=state_of({"axiom-appkit": (False, 40)})) == []


def test_a_missing_tree_belongs_to_the_other_check(tmp_path):
    """check_trees owns absence. Two findings for one fact reads as two
    problems."""
    assert check_pins(lane(["gone"]), tmp_path, state=state_of({})) == []


def test_something_that_is_not_a_checkout_is_not_judged(tmp_path):
    (tmp_path / "notgit").mkdir()
    assert check_pins(lane(["notgit"]), tmp_path, state=lambda _p: None) == []


def test_every_pinned_tree_is_checked_not_just_the_first(tmp_path):
    for n in ("axiom-service", "nos-service", "appkit-service"):
        (tmp_path / n).mkdir()
    findings = check_pins(
        lane(["axiom-service", "nos-service", "appkit-service"]),
        tmp_path,
        state=state_of({"axiom-service": (True, 9), "nos-service": (True, 0),
                        "appkit-service": (True, 2)}),
    )

    assert {f.subject for f in findings} == {"standing:axiom-service", "standing:appkit-service"}


def test_an_absolute_tree_path_is_honoured_as_given(tmp_path):
    d = tmp_path / "elsewhere"
    d.mkdir()
    findings = check_pins(lane([str(d)]), Path("/unused"), state=state_of({"elsewhere": (True, 1)}))

    assert len(findings) == 1
