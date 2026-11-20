# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET names the lane a merge belonged to. It infers nothing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.lane.registry import Lane
from axiom.extensions.builtins.release import lane_signal


LANES = {
    "chat": Lane(name="chat", front=8802, api=8803, database="axiom_lane_chat",
                 branch="feat/chat-surface-parity"),
    "steer": Lane(name="steer", front=8800, api=8801, database="axiom_lane_steer",
                  branch="feat/steer-surface"),
}


def test_a_branch_finds_its_lane():
    assert lane_signal.lane_for_branch("feat/steer-surface", LANES).name == "steer"


def test_an_unclaimed_branch_finds_nothing():
    assert lane_signal.lane_for_branch("feat/something-else", LANES) is None


def test_matching_is_exact_not_fuzzy():
    """A near-match is worse than none: acting on the wrong lane is the
    failure this is meant to prevent."""
    assert lane_signal.lane_for_branch("feat/steer", LANES) is None
    assert lane_signal.lane_for_branch("feat/steer-surface-r2", LANES) is None


def test_an_empty_branch_is_not_a_match():
    assert lane_signal.lane_for_branch("", LANES) is None


def test_an_event_gains_the_lane_it_belonged_to():
    out = lane_signal.annotate({"event": "rivet.pr_merged", "branch": "feat/chat-surface-parity"}, LANES)

    assert out["lane"] == "chat"
    assert out["lane_database"] == "axiom_lane_chat"
    assert out["event"] == "rivet.pr_merged", "the original event survives"


def test_an_event_with_no_lane_gains_no_empty_keys():
    """So a consumer can tell 'no lane' from 'lane unknown'."""
    out = lane_signal.annotate({"event": "rivet.pr_merged", "branch": "feat/other"}, LANES)

    assert "lane" not in out
    assert out == {"event": "rivet.pr_merged", "branch": "feat/other"}


def test_annotate_does_not_mutate_its_input():
    event = {"event": "rivet.pr_merged", "branch": "feat/chat-surface-parity"}
    lane_signal.annotate(event, LANES)

    assert "lane" not in event


# ---- branch_landed: RIVET's authoritative merge check -----------------------
#
# This is the one judgement TIDY is forbidden to make for itself (ADR-046), so
# it lives here and TIDY injects it. The invariant under test is the asymmetry:
# True only when truly merged, None ("cannot tell") for every doubt, because
# the None path is the one that blocks reclamation.


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "checkout", "-q", "-b", "main")
    (r / "f").write_text("1\n")
    _git(r, "add", "f")
    _git(r, "commit", "-q", "-m", "base")
    return r


def test_a_merged_branch_reads_as_landed(repo: Path):
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "f").write_text("2\n")
    _git(repo, "commit", "-qam", "work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feat/x")

    assert lane_signal.branch_landed("feat/x", repo=repo) is True


def test_an_unmerged_branch_reads_as_not_landed(repo: Path):
    _git(repo, "checkout", "-q", "-b", "feat/y")
    (repo / "f").write_text("3\n")
    _git(repo, "commit", "-qam", "work")

    assert lane_signal.branch_landed("feat/y", repo=repo) is False


def test_an_unknown_branch_cannot_be_told(repo: Path):
    assert lane_signal.branch_landed("feat/never", repo=repo) is None


def test_no_repo_cannot_be_told():
    assert lane_signal.branch_landed("feat/x", repo=None) is None
    assert lane_signal.branch_landed("", repo=Path(".")) is None


def test_the_probe_binds_each_branch_to_its_own_checkout(repo: Path):
    """The closure resolves a branch to the checkout its lane recorded, and a
    lane with no on-disk root cannot be told rather than guessing elsewhere."""
    _git(repo, "checkout", "-q", "-b", "feat/z")
    (repo / "f").write_text("4\n")
    _git(repo, "commit", "-qam", "work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feat/z")

    lanes = {
        "z": Lane(name="z", front=8820, api=8821, branch="feat/z", root=str(repo)),
        "ghost": Lane(name="ghost", front=8822, api=8823, branch="feat/ghost",
                      root=str(repo / "does-not-exist")),
    }
    probe = lane_signal.landed_probe(lanes)

    assert probe("feat/z") is True
    assert probe("feat/ghost") is None
    assert probe("feat/unclaimed") is None
