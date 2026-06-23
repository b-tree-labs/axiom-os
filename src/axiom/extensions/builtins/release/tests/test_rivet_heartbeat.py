# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RIVET's proactive heartbeat — wires the persona TODO."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from axiom.extensions.builtins.release import agent_cli
from axiom.extensions.builtins.release.ci_monitor import PipelineStatus


@contextmanager
def _stub_state_dir(tmp_path: Path):
    """Redirect heartbeat state writes to tmp_path via ``AXI_STATE_DIR``.

    The heartbeat reaches state via ``get_user_state_dir`` from many
    modules (``_legacy_rivet_cli``, ``routine_monitor``, ``pr_check_*``).
    Patching the symbol on the ``agent_cli`` shim only rebinds it on
    ``agent_cli``; the other callers keep the original reference. The
    env-var override at the source of ``get_user_state_dir`` redirects
    every caller consistently.
    """
    prev = os.environ.get("AXI_STATE_DIR")
    os.environ["AXI_STATE_DIR"] = str(tmp_path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AXI_STATE_DIR", None)
        else:
            os.environ["AXI_STATE_DIR"] = prev


def _stub_sync():
    """Stub local-main sync so the heartbeat unit tests stay hermetic.

    The heartbeat fetches + fast-forwards every workspace repo; left live,
    a dev machine with ``$AXI_WORKSPACE_ROOT`` set would do dozens of real
    network fetches during a unit test. ``local_sync`` has its own coverage
    in ``test_rivet_local_sync``; here we only care about the signal shape.
    """
    return patch(
        "axiom.extensions.builtins.release.local_sync.sync_workspace",
        return_value=[],
    )


@contextmanager
def _stub_pr_watch():
    """Stub PR-scoped CI watch + cross-repo trunk watch so heartbeat
    tests don't hit the network. Without this, the heartbeat shells out
    to ``gh`` and surfaces real failing-PR flips, escalating exit to 2
    and contaminating the signal shape under test."""
    with (
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


def test_heartbeat_subcommand_in_parser():
    parser = agent_cli.build_parser()
    args = parser.parse_args(["heartbeat"])
    assert args.action == "heartbeat"


def test_heartbeat_writes_jsonl_signal_when_all_green(tmp_path):
    pipelines = [
        PipelineStatus(repo="axiom", provider="github", ref="main", status="success"),
        PipelineStatus(repo="example-consumer", provider="gitlab", ref="main", status="success"),
    ]
    with (
        _stub_state_dir(tmp_path),
        _stub_sync(),
        _stub_pr_watch(),
        patch(
            "axiom.extensions.builtins.release.ci_monitor.check_pipelines",
            return_value=pipelines,
        ),
    ):
        rc = agent_cli.main(["heartbeat"])

    assert rc == 0
    log = tmp_path / "agents" / "rivet" / "heartbeat.jsonl"
    assert log.exists(), "heartbeat must persist a signal entry"
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(entries) == 1
    e = entries[0]
    assert e["agent"] == "rivet"
    assert e["all_green"] is True
    assert {p["repo"] for p in e["pipelines"]} == {"axiom", "example-consumer"}
    assert e["unmatched_failures"] == []
    assert e["matched_failures"] == []


def test_heartbeat_matches_known_failure_pattern(tmp_path):
    failed = PipelineStatus(
        repo="axiom",
        provider="github",
        ref="main",
        status="failure",
        url="https://example/runs/1",
        failure_reason=(
            "SyntaxError: f-string expression part cannot include a backslash"
        ),
    )
    with (
        _stub_state_dir(tmp_path),
        _stub_sync(),
        _stub_pr_watch(),
        patch(
            "axiom.extensions.builtins.release.ci_monitor.check_pipelines",
            return_value=[failed],
        ),
    ):
        rc = agent_cli.main(["heartbeat"])

    # Non-zero exit on red so a launchd timer surfaces it in logs.
    assert rc == 2
    log = tmp_path / "agents" / "rivet" / "heartbeat.jsonl"
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["all_green"] is False
    matched = entry["matched_failures"]
    assert len(matched) == 1
    assert matched[0]["repo"] == "axiom"
    assert matched[0]["pattern"]  # pattern name surfaced
    assert matched[0]["fix"]
    assert entry["unmatched_failures"] == []


def test_heartbeat_records_unmatched_failure_for_routing(tmp_path):
    failed = PipelineStatus(
        repo="axiom",
        provider="github",
        ref="main",
        status="failure",
        failure_reason="Some never-before-seen exotic flake in the build",
    )
    with (
        _stub_state_dir(tmp_path),
        _stub_sync(),
        _stub_pr_watch(),
        patch(
            "axiom.extensions.builtins.release.ci_monitor.check_pipelines",
            return_value=[failed],
        ),
    ):
        rc = agent_cli.main(["heartbeat"])

    assert rc == 2
    log = tmp_path / "agents" / "rivet" / "heartbeat.jsonl"
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["matched_failures"] == []
    assert len(entry["unmatched_failures"]) == 1
    u = entry["unmatched_failures"][0]
    assert u["repo"] == "axiom"
    assert "exotic" in u["failure_reason"]
    # next_route documents the Bonsai-first routing the daemon will hand off to.
    assert u["next_route"] == "bonsai"


def test_heartbeat_handles_empty_pipeline_list(tmp_path):
    """No CI configured (no gh, no GITLAB_TOKEN) is a valid steady state, not an error."""
    with (
        _stub_state_dir(tmp_path),
        _stub_sync(),
        _stub_pr_watch(),
        patch(
            "axiom.extensions.builtins.release.ci_monitor.check_pipelines",
            return_value=[],
        ),
    ):
        rc = agent_cli.main(["heartbeat"])

    assert rc == 0
    entry = json.loads(
        (tmp_path / "agents" / "rivet" / "heartbeat.jsonl").read_text().splitlines()[-1]
    )
    assert entry["all_green"] is True
    assert entry["pipelines"] == []
