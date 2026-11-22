# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""This checkout's RIVET config lands eligible pull requests and reports holds."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.release.lander.config import load_config, parse_config
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused

REPO = Path(__file__).resolve().parents[6]
CONFIG = REPO / ".axiom" / "release-lander.toml"
WORKFLOW = REPO / ".github" / "workflows" / "rivet-lander.yml"


def test_claim_ttl_is_read_from_the_claims_table():
    parsed = parse_config(
        "[claims]\n"
        'label = "rivet:claimed"\n'
        "claim_ttl_s = 1800\n"
        'foreign_labels = ["janna:working"]\n'
    )
    assert parsed.claim_ttl_s == 1800
    assert parsed.claim_label == "rivet:claimed"
    assert parsed.foreign_claim_labels == ("janna:working",)
    assert parse_config("claim_ttl_s = 120\n").claim_ttl_s == 120
    assert parse_config("claim_ttl_s = 120\n[claims]\nclaim_ttl_s = 90\n").claim_ttl_s == 90
    with pytest.raises(LanderRefused, match="claim_ttl_s"):
        parse_config("[claims]\nclaim_ttl_s = 0\n")


def test_this_repo_config_is_warn_only_handoff(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    loaded = load_config(tmp_path, "owner/name")
    assert loaded.source.endswith(".axiom/release-lander.toml")
    text = CONFIG.read_text(encoding="utf-8")
    assert "https://" not in text
    assert "ghp_" not in text
    assert "github_pat_" not in text
    assert loaded.mode == "warn"
    assert loaded.fix_provider == "handoff"
    assert loaded.webhook_url_env == "RELEASE_FIX_WEBHOOK_URL"
    assert loaded.webhook_key_env == "RELEASE_FIX_WEBHOOK_KEY"
    assert loaded.webhook_key_header == "X-Fix-Key"
    assert loaded.separate_reviewer is True
    assert loaded.auto_approver == ""
    assert loaded.auto_allowed == ("merge", "ci_start")
    assert loaded.approvers == (
        "@benjaminbooth:github",
        "@redrossa:github",
        "@luker-robinson2:github",
    )
    assert loaded.approval_ttl_hours == 72
    assert loaded.claim_label == "rivet:claimed"
    assert loaded.claim_ttl_s == 3600
    assert loaded.self_landers == ("benjaminbooth",)
    assert "janna:working" in loaded.foreign_claim_labels
    assert loaded.repos == ()
    assert "may land" in text
    assert "protected path is held" in text
    assert 'mode = "warn"' in text
    for name in ("behavior", "pricing", "security", "data", "test-setup", "workflow", "dependency"):
        assert loaded.extra_path_rules[name], name
    assert any("package-lock" in pattern for pattern in loaded.extra_path_rules["dependency"])
    assert any("requirements" in pattern for pattern in loaded.extra_path_rules["dependency"])


def test_the_warn_comment_states_the_report_and_does_not_merge():
    import importlib.util

    path = REPO / ".github" / "scripts" / "rivet_warn_comment.py"
    spec = importlib.util.spec_from_file_location("rivet_warn_comment", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    body = module.comment_body("needs a person:\n  #4 waiting on ada\n")
    assert module.MARKER in body
    assert "pending human review" in body
    assert "not landed" in body
    assert "pr merge" not in body
    assert "--act" not in body


def test_the_workflow_lands_and_reports():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "may land" in text
    assert "protected path is held" in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert "ubuntu-24.04" in text
    assert "contents: write" in text
    assert "checks: read" in text
    assert "statuses: read" in text
    assert "actions: read" in text
    assert "pull-requests: write" in text
    assert "checks: write" not in text
    assert "actions: write" not in text
    assert "::error::" in text
    assert 'gh label create "rivet:claimed"' in text
    assert "AXIOM_LANDER_KILL" in text
    assert "git push" not in text
    assert "pr merge" not in text
    assert "--force" not in text
    parts = text.split("python3 -m axiom.extensions.builtins.release land")
    assert len(parts) == 3
    report_cmd = parts[1].split(">", 1)[0]
    act_cmd = parts[2]
    assert "--act" not in report_cmd
    assert "--act" in act_cmd
    assert "--propose" not in report_cmd
    assert "--config .axiom/release-lander.toml" in report_cmd
    assert "--match-head-commit" not in act_cmd
    assert "pytest" not in text.split("jobs:", 1)[1]


def test_the_land_step_merges_with_the_app_token_so_main_ci_runs():
    """A merge made with the job token starts no workflow, so main's CI never ran on what
    RIVET landed (#2020, #2237) and its revert-a-red-main watch could not see it. The app
    token is the identity the forge starts workflows for. It is also what a branch update
    needs for the fresh CI a refreshed pull request is waiting on."""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("- name: Land eligible pull requests")
    step = text[start : text.index("\n  watch-main:", start)]
    assert "GH_TOKEN: ${{ steps.rivet.outputs.token || github.token }}" in step


def test_the_lander_runs_when_ci_finishes_and_never_cancels_a_landing():
    """A green pull request waited for the next merge or the six-hourly schedule before
    RIVET looked at it, so people merged by hand and the backlog grew. RIVET now runs when
    the CI workflow completes. Landing runs queue and never cancel each other: a cancelled
    run could stop between the merge and its bookkeeping."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_run:" in text
    assert 'workflows: ["CI"]' in text
    assert "types: [completed]" in text
    land = text[text.index("  land:") : text.index("\n  watch-main:")]
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in land
    assert "cancel-in-progress: true" not in land
