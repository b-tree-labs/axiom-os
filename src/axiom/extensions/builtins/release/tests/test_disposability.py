# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET derives keep/disposable artifact verdicts from CI and the lander."""

from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone

from axiom.extensions.builtins.release.disposability import hints_from_ci_report, hints_from_lander
from axiom.infra.artifact_verdicts import load_verdicts
from axiom.extensions.builtins.release.disposability import publish_verdicts


NOW = datetime(2026, 11, 21, 15, 30, tzinfo=timezone.utc)


def test_hints_from_ci_report_cover_superseded_flake_and_queue():
    run = SimpleNamespace(id=11, repo="acme/r", head_sha="abc", pr_numbers=(), event="push", head_branch="main", workflow="CI", path=".github/workflows/ci.yml", active=False)
    queued = SimpleNamespace(id=12, repo="acme/r", head_sha="def", pr_numbers=(3,), event="merge_group", head_branch="gh-readonly-queue/main", workflow="CI", path="", active=True)
    report = SimpleNamespace(
        cancels=[SimpleNamespace(run=run, cancel=True, reason="superseded by 99")],
        flakes=[SimpleNamespace(repo="acme/r", run_id=21, sha="fff", verdict="candidate")],
        culprits=[SimpleNamespace(open=True, run=SimpleNamespace(id=22, repo="acme/r", head_sha="eee"))],
        heads={"acme/r": SimpleNamespace(run_id=23, sha="111", verdict="pending", run_attempt=1)},
        pipeline_runs=[queued],
    )
    hints = hints_from_ci_report(report, now=NOW)
    by_reason = {h.reason: h for h in hints}
    assert by_reason["superseded_green"].verdict == "disposable"
    assert by_reason["flaky_investigation"].verdict == "keep"
    assert by_reason["failing_run_diagnosed"].verdict == "keep"
    assert by_reason["pr_in_flight"].verdict == "keep"
    assert by_reason["merge_queue"].verdict == "keep"


def test_hints_from_lander_open_merged_and_bisect():
    open_pr = SimpleNamespace(head_sha="aaa", merge_state="QUEUED")
    hints = hints_from_lander(
        repo="acme/r",
        pulls=[open_pr],
        lock_held=True,
        bisect_shas=["bbb"],
        merged=["ccc"],
        closed=["ddd"],
        now=NOW,
    )
    by_reason = {h.reason: h for h in hints}
    assert by_reason["merge_queue"].head_sha == "aaa"
    assert by_reason["release_pending"].verdict == "keep"
    assert by_reason["bisect"].verdict == "keep"
    assert by_reason["pr_merged"].verdict == "disposable"
    assert by_reason["pr_closed"].verdict == "disposable"


def test_publish_swallows_nothing_on_success(tmp_path):
    hints = hints_from_lander(repo="acme/r", pulls=[SimpleNamespace(head_sha="abc", merge_state="")], now=NOW)
    assert publish_verdicts(tmp_path, hints, now=NOW) == 1
    loaded = load_verdicts(tmp_path)
    assert loaded.lookup(head_sha="abc", repo="acme/r", now=NOW).reason == "pr_in_flight"
