# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RIVET's cloud-routine watcher."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from axiom.extensions.builtins.release import agent_cli, routine_monitor
from axiom.extensions.builtins.release.ci_monitor import PipelineStatus


@contextmanager
def _stub_state_dir(tmp_path: Path):
    """Redirect state via ``AXI_STATE_DIR``. Patching the symbol on the
    ``agent_cli`` shim only rebinds it there; ``_legacy_rivet_cli`` and
    ``routine_monitor`` keep the original ``get_user_state_dir``
    reference. The env-var is the one redirection that covers every
    caller consistently."""
    prev = os.environ.get("AXI_STATE_DIR")
    os.environ["AXI_STATE_DIR"] = str(tmp_path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AXI_STATE_DIR", None)
        else:
            os.environ["AXI_STATE_DIR"] = prev


@contextmanager
def _stub_heartbeat_externals():
    """Stub sync + PR watchers so heartbeat tests stay hermetic."""
    with (
        patch(
            "axiom.extensions.builtins.release.local_sync.sync_workspace",
            return_value=[],
        ),
        patch(
            "axiom.extensions.builtins.release.pr_check_watcher.watch_user_prs",
            return_value=[],
        ),
        patch(
            "axiom.extensions.builtins.release.cross_repo_pr_watch.load_watched_repos",
            return_value=[],
        ),
    ):
        yield


def test_track_creates_entry(tmp_path):
    r = routine_monitor.track(
        tmp_path, trigger_id="trig_xyz", branch="feat/foo", note="rev-u"
    )
    assert r.trigger_id == "trig_xyz"
    assert r.state == "pending"
    loaded = routine_monitor.load_tracked(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].trigger_id == "trig_xyz"


def test_track_is_idempotent(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_xyz", branch="feat/foo")
    routine_monitor.track(tmp_path, trigger_id="trig_xyz", branch="feat/foo")
    assert len(routine_monitor.load_tracked(tmp_path)) == 1


def test_untrack_removes_entry(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_a", branch="feat/a")
    routine_monitor.track(tmp_path, trigger_id="trig_b", branch="feat/b")
    assert routine_monitor.untrack(tmp_path, "trig_a") is True
    assert routine_monitor.untrack(tmp_path, "trig_missing") is False
    remaining = routine_monitor.load_tracked(tmp_path)
    assert [r.trigger_id for r in remaining] == ["trig_b"]


def test_poll_emits_branch_seen_transition(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_x", branch="feat/foo")

    with (
        patch.object(routine_monitor, "_gh_branch_exists", return_value=True),
        patch.object(routine_monitor, "_gh_pr_for_branch", return_value=None),
    ):
        transitions = routine_monitor.poll_routines(tmp_path)

    assert len(transitions) == 1
    t = transitions[0]
    assert t["from"] == "pending"
    assert t["to"] == "branch_seen"


def test_poll_emits_pr_opened_transition(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_x", branch="feat/foo")

    with (
        patch.object(routine_monitor, "_gh_branch_exists", return_value=True),
        patch.object(
            routine_monitor,
            "_gh_pr_for_branch",
            return_value={"number": 200, "state": "OPEN", "isDraft": True},
        ),
    ):
        transitions = routine_monitor.poll_routines(tmp_path)

    assert any(t["to"] == "pr_opened" and t["pr_number"] == 200 for t in transitions)


def test_poll_marks_completed_on_merged_pr(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_x", branch="feat/foo")

    with (
        patch.object(routine_monitor, "_gh_branch_exists", return_value=True),
        patch.object(
            routine_monitor,
            "_gh_pr_for_branch",
            return_value={"number": 9, "state": "MERGED", "isDraft": False},
        ),
    ):
        routine_monitor.poll_routines(tmp_path)

    after = routine_monitor.load_tracked(tmp_path)
    assert after[0].state == "completed"


def test_poll_is_quiet_on_no_change(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_x", branch="feat/foo")

    with (
        patch.object(routine_monitor, "_gh_branch_exists", return_value=False),
        patch.object(routine_monitor, "_gh_pr_for_branch", return_value=None),
    ):
        first = routine_monitor.poll_routines(tmp_path)
        second = routine_monitor.poll_routines(tmp_path)

    assert first == []
    assert second == []


def test_watch_routine_cli(tmp_path):
    with _stub_state_dir(tmp_path):
        rc = agent_cli.main(
            [
                "watch",
                "routine",
                "trig_test",
                "--branch",
                "feat/test",
                "--note",
                "smoke",
            ]
        )
    assert rc == 0
    routines = routine_monitor.load_tracked(tmp_path)
    assert len(routines) == 1
    assert routines[0].trigger_id == "trig_test"
    assert routines[0].note == "smoke"


def test_watched_cli_lists(tmp_path, capsys):
    routine_monitor.track(tmp_path, trigger_id="trig_test", branch="feat/test")
    with _stub_state_dir(tmp_path):
        rc = agent_cli.main(["watched"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "trig_test" in out
    assert "feat/test" in out


def test_unwatch_cli(tmp_path, capsys):
    routine_monitor.track(tmp_path, trigger_id="trig_test", branch="feat/test")
    with _stub_state_dir(tmp_path):
        rc = agent_cli.main(["unwatch", "trig_test"])
    assert rc == 0
    assert routine_monitor.load_tracked(tmp_path) == []


def test_heartbeat_includes_routine_transitions(tmp_path):
    routine_monitor.track(tmp_path, trigger_id="trig_x", branch="feat/foo")
    pipelines = [
        PipelineStatus(repo="axiom", provider="github", ref="main", status="success")
    ]
    with (
        _stub_state_dir(tmp_path),
        _stub_heartbeat_externals(),
        patch(
            "axiom.extensions.builtins.release.ci_monitor.check_pipelines",
            return_value=pipelines,
        ),
        patch.object(routine_monitor, "_gh_branch_exists", return_value=True),
        patch.object(
            routine_monitor,
            "_gh_pr_for_branch",
            return_value={"number": 42, "state": "OPEN", "isDraft": True},
        ),
    ):
        rc = agent_cli.main(["heartbeat"])

    assert rc == 0
    log = tmp_path / "agents" / "rivet" / "heartbeat.jsonl"
    entry = json.loads(log.read_text().splitlines()[-1])
    assert "routine_transitions" in entry
    kinds = {t["to"] for t in entry["routine_transitions"]}
    assert "pr_opened" in kinds
