# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A finished pull request can wait for its owner without being a draft.

The lander lands what is green and ready, so the only way to keep a finished
pull request from landing was to leave it a draft, which also skips the unit
suite and reads as unfinished. A hold label says "finished, waiting for its
owner": the lander lands nothing, claims nothing, updates nothing, fixes
nothing and starts no CI on it, lists it in its own group with its age, and
takes it up again on the first pass after the label is gone.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix
from axiom.extensions.builtins.release.lander import ci_start, owner_hold
from axiom.extensions.builtins.release.lander.actor import fixer_block, landing_block, run_actor
from axiom.extensions.builtins.release.lander.checks import CI_NEVER_RAN
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.owner_hold import HELD_FOR_OWNER_REVIEW
from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
REPO = "owner/name"
HEAD = "a" * 40
LABEL = "hold: owner review"
CLAIM = "rivet:claimed"


class Effects:
    """Records every write the lander could make to a pull request."""

    def __init__(self) -> None:
        self.landed: list[int] = []
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.started: list[int] = []
        self.updated: list[int] = []
        self.applied: list[int] = []
        self.closed: list[int] = []

    def head(self, number: int) -> str:
        del number
        return HEAD

    def apply(self, number: int, fix: Fix, head_sha: str) -> str:
        del fix, head_sha
        self.applied.append(number)
        return "b" * 40

    def rerun(self, number: int, sha: str) -> str:
        del number, sha
        return "passing"

    def land(self, number: int, sha: str, method: str) -> None:
        del sha, method
        self.landed.append(number)

    def close(self, number: int, sha: str) -> None:
        del sha
        self.closed.append(number)

    def add_label(self, number: int, label: str) -> None:
        self.added.append((number, label))

    def remove_label(self, number: int, label: str) -> None:
        self.removed.append((number, label))

    def start_ci(self, number: int, sha: str, *, token_env: str = "") -> None:
        del sha, token_env
        self.started.append(number)

    def update_branch(self, number: int, sha: str) -> str:
        del sha
        self.updated.append(number)
        return "c" * 40

    @property
    def writes(self) -> list:
        return [
            *self.landed,
            *self.added,
            *self.removed,
            *self.started,
            *self.updated,
            *self.applied,
            *self.closed,
        ]


class Fixes:
    """Clear when green; otherwise a diff RIVET may apply on its own. Counts every ask."""

    name = "fixes"

    def __init__(self) -> None:
        self.asked: list[int] = []

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        self.asked.append(pull.number)
        if pull.checks == "passing" and pull.mergeable is not False:
            return Diagnosis("clear", "checks passing", pull.files)
        return Diagnosis("lint", "red", pull.files)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        del pull
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        return Fix(summary="fix lint", diff="--- a\n+++ b\n", kind="repair", paths=("src/a.py",))


_EVERYTHING = ("merge", "ci_start", "update_branch", "repair", "rerun")


def _config(**kwargs) -> LanderConfig:
    return replace(LanderConfig(), approvers=("@ada:local",), auto_allowed=_EVERYTHING, **kwargs)


def _pull(**kwargs) -> OpenPull:
    base: dict = dict(
        number=7,
        title="change",
        head_sha=HEAD,
        head_ref="topic/one",
        files=("src/a.py",),
        checks="passing",
        mergeable=True,
        created_at=NOW - timedelta(days=2, hours=3),
        updated_at=NOW - timedelta(hours=1),
    )
    base.update(kwargs)
    return OpenPull(**base)


def _act(tmp_path: Path, pulls, effects=None, config=None, provider=None):
    effects = effects or Effects()
    result = run_actor(
        pulls,
        repo=REPO,
        config=config or _config(),
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=provider or Fixes(),
        environ={},
    )
    return result, effects


class _All:
    def select(self, paths, base, head):
        from axiom.extensions.builtins.release.selectors import Selection

        del paths, base, head
        return Selection(True, (), "all")


def _report(tmp_path: Path, pulls, config=None):
    return build_report(
        pulls,
        repo=REPO,
        config=config or _config(),
        state_dir=tmp_path,
        tier_map=None,
        selector=_All(),
        now=NOW,
    )


# --- the hold --------------------------------------------------------------


def test_the_default_label_holds_and_an_unlabelled_pull_request_is_not_held():
    config = _config()
    assert config.hold_labels == (LABEL,)
    held = owner_hold.hold_for(_pull(labels=(LABEL,)), config, repo=REPO)
    assert held is not None and held.reason == HELD_FOR_OWNER_REVIEW and held.by == "label"
    assert LABEL in held.why
    assert owner_hold.hold_for(_pull(labels=("bug",)), config, repo=REPO) is None
    # A forge treats label names as one name whatever the case.
    assert owner_hold.hold_for(_pull(labels=("Hold: Owner Review",)), config, repo=REPO) is not None


def test_a_review_request_to_the_owner_is_a_hold_and_one_to_anybody_else_is_not():
    config = _config()
    # With no owners listed, the owner is the account the repository belongs to.
    held = owner_hold.hold_for(_pull(pending_human_reviewers=("Owner",)), config, repo=REPO)
    assert held is not None and held.by == "review_request" and "Owner" in held.why
    assert owner_hold.hold_for(_pull(pending_human_reviewers=("bea",)), config, repo=REPO) is None
    listed = _config(hold_owners=("@bea",))
    assert (
        owner_hold.hold_for(_pull(pending_human_reviewers=("bea",)), listed, repo=REPO) is not None
    )
    assert owner_hold.hold_for(_pull(pending_human_reviewers=("owner",)), listed, repo=REPO) is None
    off = _config(hold_on_owner_review_request=False)
    assert owner_hold.hold_for(_pull(pending_human_reviewers=("owner",)), off, repo=REPO) is None


# --- act -------------------------------------------------------------------


def test_held_green_and_ready_is_not_landed(tmp_path: Path):
    result, effects = _act(tmp_path, [_pull(labels=(LABEL,))])
    outcome = result.outcomes[0]
    assert outcome.status == "held"
    assert outcome.reason.startswith(HELD_FOR_OWNER_REVIEW)
    assert effects.landed == []
    assert effects.writes == []


def test_a_held_draft_is_left_alone_too(tmp_path: Path):
    result, effects = _act(tmp_path, [_pull(labels=(LABEL,), draft=True)])
    assert result.outcomes[0].status == "held"
    assert effects.writes == []


def test_held_is_not_claimed_updated_fixed_or_reopened(tmp_path: Path):
    provider = Fixes()
    behind = _pull(number=1, labels=(LABEL,), merge_state="BEHIND")
    red = _pull(number=2, labels=(LABEL,), checks="failing", failure_log="lint")
    never_ran = _pull(number=3, labels=(LABEL,), checks="pending", ci_never_ran=True)
    result, effects = _act(tmp_path, [behind, red, never_ran], provider=provider)
    assert [item.status for item in result.outcomes] == ["held", "held", "held"]
    assert effects.added == []  # not claimed
    assert effects.updated == []  # branch not updated
    assert effects.applied == [] and provider.asked == []  # the fixer is never asked
    assert effects.started == [] and effects.closed == []  # no close and reopen
    assert ci_start.starts(tmp_path, REPO, 3) == []
    assert effects.writes == []


def test_a_review_request_to_the_owner_holds_the_same_way(tmp_path: Path):
    provider = Fixes()
    pull = _pull(pending_human_reviewers=("owner",), merge_state="BEHIND")
    result, effects = _act(tmp_path, [pull], provider=provider)
    assert result.outcomes[0].status == "held"
    assert effects.writes == [] and provider.asked == []


def test_an_unheld_pull_request_is_unaffected(tmp_path: Path):
    held = _pull(number=1, labels=(LABEL,))
    free = _pull(number=2, labels=("bug",))
    result, effects = _act(tmp_path, [held, free])
    assert {item.number: item.status for item in result.outcomes} == {1: "held", 2: "landed"}
    assert effects.landed == [2]
    assert effects.added == [(2, CLAIM)] and effects.removed == [(2, CLAIM)]


def test_a_held_pull_request_does_not_take_the_one_branch_update(tmp_path: Path):
    # The older pull request would be "next to land"; held, it must not make the other wait.
    held = _pull(
        number=1, labels=(LABEL,), merge_state="BEHIND", created_at=NOW - timedelta(days=9)
    )
    free = _pull(number=2, merge_state="BEHIND")
    result, effects = _act(tmp_path, [held, free])
    assert effects.updated == [2]
    assert {item.number: item.status for item in result.outcomes} == {1: "held", 2: "updated"}


def test_removing_the_label_lands_it_on_the_next_pass(tmp_path: Path):
    first, held_effects = _act(tmp_path, [_pull(labels=(LABEL,))])
    assert first.outcomes[0].status == "held" and held_effects.landed == []
    second, effects = _act(tmp_path, [_pull(labels=())])
    assert second.outcomes[0].status == "landed"
    assert effects.landed == [7]


def test_a_claim_an_earlier_pass_left_is_given_back_when_the_hold_arrives(tmp_path: Path):
    result, effects = _act(tmp_path, [_pull(labels=(LABEL, CLAIM))])
    assert result.outcomes[0].status == "held"
    assert effects.added == [] and effects.removed == [(7, CLAIM)]
    assert effects.landed == []


def test_the_blocks_name_the_hold_before_any_other_reason():
    config = _config()
    held = _pull(labels=(LABEL,), draft=True)
    assert fixer_block(held, config).startswith(HELD_FOR_OWNER_REVIEW)
    assert landing_block(held, config).startswith(HELD_FOR_OWNER_REVIEW)
    assert fixer_block(_pull(), config) == ""


def test_the_act_pass_files_it_where_the_lander_already_raises_things_to_a_person(tmp_path: Path):
    _act(tmp_path, [_pull(labels=(LABEL,))])
    lines = (
        (tmp_path / "agents" / "rivet" / "lander.jsonl").read_text(encoding="utf-8").splitlines()
    )
    rows = [json.loads(line) for line in lines]
    raised = [row for row in rows if row.get("action") == "needs-person"]
    assert len(raised) == 1
    assert raised[0]["category"] == HELD_FOR_OWNER_REVIEW
    assert raised[0]["pr"] == 7 and raised[0]["next_route"] == "human"


# --- report ----------------------------------------------------------------


def test_the_report_groups_held_pull_requests_with_an_age(tmp_path: Path):
    held = _pull(labels=(LABEL,), title="finished work")
    free = _pull(number=8, head_ref="topic/two", files=("src/b.py",))
    report = _report(tmp_path, [held, free])
    # Green and ready, and still not in "ready to land".
    assert [item.number for item in report.ready] == [8]
    # It is in the group a person reads, under its own reason.
    person = {item.number: item for item in report.needs_human}
    assert person[7].reasons[0] == HELD_FOR_OWNER_REVIEW
    rows = report.to_dict()["held_for_owner_review"]
    assert [row["number"] for row in rows] == [7]
    row = rows[0]
    assert row["reason"] == HELD_FOR_OWNER_REVIEW and row["held_by"] == "label"
    assert row["age_hours"] == 51.0
    # The label time is not in what the lander already reads, so the age is the pull request's.
    assert row["age_from"] == "pull_request_opened"
    text = render_text(report)
    assert "Waiting for owner review (1):" in text
    line = next(line for line in text.splitlines() if line.startswith("  #7 open 2d 3h"))
    assert LABEL in line and "finished work" in line
    assert "#8" not in text.split("Waiting for owner review (1):", 1)[1].split("\n\n", 1)[0]


def test_a_report_with_no_hold_has_no_such_group(tmp_path: Path):
    report = _report(tmp_path, [_pull()])
    assert report.to_dict()["held_for_owner_review"] == []
    assert "Waiting for owner review" not in render_text(report)


def test_a_held_head_ci_never_ran_on_is_not_promised_a_start(tmp_path: Path):
    pull = _pull(labels=(LABEL,), checks="pending", ci_never_ran=True, missing_checks=("Lint",))
    chosen = ci_start.plan(pull, _config(), state_dir=tmp_path, repo=REPO, environ={})
    assert chosen.next != ci_start.RIVET
    report = _report(tmp_path, [pull])
    reasons = report.needs_human[0].reasons
    assert reasons[0] == HELD_FOR_OWNER_REVIEW and CI_NEVER_RAN in reasons
    assert "RIVET starts it once on its next" not in render_text(report)


# --- config ----------------------------------------------------------------


def test_config_can_rename_the_label_and_add_more(tmp_path: Path):
    config = parse_config(
        '[raci]\napprovers = ["@ada:local"]\nauto_allowed = ["merge"]\n'
        '[hold]\nlabels = ["waiting: owner", "hold: release train"]\n'
        'owner_review_request = false\nowners = ["bea"]\n'
    )
    assert config.hold_labels == ("waiting: owner", "hold: release train")
    assert config.hold_on_owner_review_request is False
    assert config.hold_owners == ("bea",)
    # The renamed label holds; the default name no longer does.
    renamed, effects = _act(tmp_path / "a", [_pull(labels=("hold: release train",))], config=config)
    assert renamed.outcomes[0].status == "held" and effects.landed == []
    default, landed = _act(tmp_path / "b", [_pull(labels=(LABEL,))], config=config)
    assert default.outcomes[0].status == "landed" and landed.landed == [7]
    row = _report(tmp_path / "c", [_pull(labels=("waiting: owner",))], config=config).to_dict()[
        "held_for_owner_review"
    ][0]
    assert "waiting: owner" in row["why"]


def test_the_defaults_and_a_bad_hold_table_is_refused():
    config = parse_config('[raci]\napprovers = ["@ada:local"]\n')
    assert config.hold_labels == (LABEL,)
    assert config.hold_on_owner_review_request is True
    assert config.hold_owners == ()
    for bad in (
        'hold = "yes"\n',
        "[hold]\nlabels = []\n",
        '[hold]\nlabels = "hold"\n',
        '[hold]\nowner_review_request = "yes"\n',
        '[hold]\nlabels = ["rivet:claimed"]\n',
    ):
        with pytest.raises(LanderRefused):
            parse_config(bad)


# --- this checkout's wiring ------------------------------------------------


def test_this_checkout_declares_the_hold_and_its_workflow_creates_the_label():
    root = Path(__file__).resolve().parents[6]
    declared = (root / ".axiom" / "release-lander.toml").read_text(encoding="utf-8")
    assert "\n[hold]\n" in declared
    config = parse_config(declared)
    assert config.hold_labels == (LABEL,)
    assert config.hold_on_owner_review_request is True
    workflow = (root / ".github" / "workflows" / "rivet-lander.yml").read_text(encoding="utf-8")
    assert f'gh label create "{LABEL}"' in workflow
