# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow mode: the lander computes what the verifier says and changes nothing (#2471).

Each pull request the lander evaluates gets one row, "would have skipped the
owner's GitHub review: yes / no / uncertain", next to the decision record. The
replay compares those rows with what the owner then did on GitHub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.agreement import hook, shadow, store

from . import _agreement_helpers as h
from .test_lander_owner_review_hold import REPO as LANDER_REPO
from .test_lander_owner_review_hold import _act, _pull

NOW = datetime.now(UTC)


def _agreed_world(tmp_path: Path):
    """An owner yes, logged by the hook and recorded, for pull request 7 as the lander test builds it."""
    projects = tmp_path / "projects"
    rows = [
        h.human("Please change src/a.py.", NOW - timedelta(hours=1)),
        h.assistant(
            "Proposed work: change src/a.py in the change. Shall I build it?",
            NOW - timedelta(minutes=59),
        ),
        h.human("build it", NOW - timedelta(minutes=58)),
        h.assistant_tool(NOW - timedelta(minutes=50), command="gh pr create --head topic/one"),
        h.tool_result(f"https://github.com/{LANDER_REPO}/pull/7", NOW - timedelta(minutes=49)),
    ]
    path = h.write_transcript(projects / "p" / "S1.jsonl", rows)
    entry_id = hook.handle(
        h.hook_payload(rows[2], path), state_dir=tmp_path, now=NOW - timedelta(minutes=58)
    )
    store.create_record(
        tmp_path,
        message_id=entry_id,
        repo=LANDER_REPO,
        pr=7,
        head_sha=h.HEAD,
        work="change src/a.py",
        paths=("src/a.py",),
        actions=(),
        now=NOW - timedelta(minutes=40),
    )
    return projects


def _outcomes(result):
    return [(o.number, o.status, o.reason, o.head_sha, tuple(o.steps)) for o in result.outcomes]


def test_shadow_changes_no_outcome_and_no_write(tmp_path: Path, monkeypatch):
    projects = _agreed_world(tmp_path / "with")
    monkeypatch.setattr(shadow, "default_projects_root", lambda: projects)
    with_log, fx_with = _act(tmp_path / "with", [_pull()])
    bare, fx_bare = _act(tmp_path / "bare", [_pull()])
    assert _outcomes(with_log) == _outcomes(bare)
    assert fx_with.writes == fx_bare.writes


def test_a_pass_with_no_prompt_log_writes_no_shadow_file(tmp_path: Path):
    _act(tmp_path, [_pull()])
    assert not (tmp_path / "agents" / "rivet" / "agreement").exists()


def test_the_shadow_row_says_yes_for_a_pull_request_the_owner_agreed_to(
    tmp_path: Path, monkeypatch
):
    projects = _agreed_world(tmp_path)
    monkeypatch.setattr(shadow, "default_projects_root", lambda: projects)
    _act(tmp_path, [_pull(), _pull(number=8, head_ref="topic/two", files=("src/b.py",))])
    rows = shadow.read_shadow(tmp_path)
    by_pr = {r["pr"]: r for r in rows}
    assert by_pr[7]["would_have_skipped_the_owners_github_review"] == "yes"
    assert by_pr[7]["decision"] == "AGREED" and by_pr[7]["record_id"]
    assert by_pr[8]["would_have_skipped_the_owners_github_review"] == "no"
    assert by_pr[7]["head_sha"] == h.HEAD and by_pr[7]["live_effect"] == "none"


def test_the_same_head_and_decision_is_recorded_once(tmp_path: Path, monkeypatch):
    projects = _agreed_world(tmp_path)
    monkeypatch.setattr(shadow, "default_projects_root", lambda: projects)
    _act(tmp_path, [_pull()])
    _act(tmp_path, [_pull()])
    assert len([r for r in shadow.read_shadow(tmp_path) if r["pr"] == 7]) == 1


def test_a_verifier_that_crashes_leaves_the_pass_alone(tmp_path: Path, monkeypatch):
    projects = _agreed_world(tmp_path / "a")
    monkeypatch.setattr(shadow, "default_projects_root", lambda: projects)

    def boom(*args, **kwargs):
        raise RuntimeError("verifier broke")

    monkeypatch.setattr(shadow, "verify", boom)
    broken, _fx = _act(tmp_path / "a", [_pull()])
    clean, _fx2 = _act(tmp_path / "b", [_pull()])
    assert _outcomes(broken) == _outcomes(clean)


# --- the replay -------------------------------------------------------------


def _row(pr, decision, head=h.HEAD, repo="o/n"):
    skip = {"AGREED": "yes", "NOT_AGREED": "no", "UNCERTAIN": "uncertain"}[decision]
    return {
        "repo": repo,
        "pr": pr,
        "head_sha": head,
        "decision": decision,
        "would_have_skipped_the_owners_github_review": skip,
        "time": "2026-01-10T12:00:00+00:00",
    }


def _gh(state="MERGED", head=h.HEAD, reviews=()):
    return {"state": state, "head_oid": head, "reviews": list(reviews)}


def _review(state, commit=h.HEAD, login="owner"):
    return {"login": login, "state": state, "commit": commit}


@pytest.mark.parametrize(
    "gh, outcome",
    [
        (_gh(reviews=[_review("APPROVED")]), "approved_unchanged"),
        (_gh(head="b" * 40, reviews=[_review("APPROVED", "b" * 40)]), "changed_after"),
        (_gh(reviews=[_review("APPROVED", "c" * 40)]), "changed_after"),
        (_gh(state="OPEN", reviews=[_review("CHANGES_REQUESTED")]), "rejected"),
        (_gh(state="CLOSED"), "rejected"),
        (_gh(state="OPEN"), "pending"),
        (_gh(reviews=[]), "merged_without_owner_review"),
        (_gh(reviews=[_review("APPROVED", login="someone-else")]), "merged_without_owner_review"),
    ],
)
def test_the_owners_outcome_on_github_is_classified(gh, outcome):
    assert shadow.owner_outcome(_row(1, "AGREED"), gh, owners=("owner",)) == outcome


def test_the_replay_counts_each_verdict_against_what_the_owner_did():
    rows = [_row(1, "AGREED"), _row(2, "AGREED"), _row(3, "NOT_AGREED"), _row(4, "UNCERTAIN")]
    gh = {
        1: _gh(reviews=[_review("APPROVED")]),
        2: _gh(state="OPEN", reviews=[_review("CHANGES_REQUESTED")]),
        3: _gh(reviews=[_review("APPROVED")]),
        4: _gh(state="OPEN"),
    }
    report = shadow.compare(rows, lambda repo, pr: gh[pr], owners=("owner",))
    assert report["would_skip"]["yes"] == {"approved_unchanged": 1, "rejected": 1}
    assert report["would_skip"]["no"] == {"approved_unchanged": 1}
    assert report["would_skip"]["uncertain"] == {"pending": 1}
    assert report["false_skips"] == [{"repo": "o/n", "pr": 2, "outcome": "rejected"}]
    assert report["ready_to_go_live"] is False
    assert "1 of 2" in report["sentence"]


def test_the_replay_never_calls_a_small_sample_ready():
    rows = [_row(i, "AGREED") for i in range(1, 6)]
    report = shadow.compare(
        rows, lambda r, p: _gh(reviews=[_review("APPROVED")]), owners=("owner",)
    )
    assert report["false_skips"] == [] and report["ready_to_go_live"] is False
    assert report["minimum_sample"] == 20


def test_the_replay_uses_the_latest_row_for_each_pull_request():
    rows = [_row(1, "UNCERTAIN", head="d" * 40), _row(1, "AGREED")]
    rows[1]["time"] = "2026-01-10T13:00:00+00:00"
    report = shadow.compare(
        rows, lambda r, p: _gh(reviews=[_review("APPROVED")]), owners=("owner",)
    )
    assert report["would_skip"] == {"yes": {"approved_unchanged": 1}}
