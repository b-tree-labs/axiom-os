# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Rivet's CI repair and review are MCP tools any harness can call."""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed, mcp_tool_name
from axiom.extensions.builtins.release.ci_flow import choose_reviewer, plan_repair
from axiom.extensions.builtins.release.skills import bind, ci_repair, review
from axiom.infra.skills import SkillContext, SkillRegistry


def test_a_context_drift_is_a_sync_then_a_push():
    plan = plan_repair("context files drifted from AGENTS.md — run axi context sync")
    assert plan["pattern"] == "context-sync"
    assert plan["resubmit"] == "push"
    assert plan["resubmit"] != "update-branch"
    assert any("context sync" in command for command in plan["commands"])


def test_a_ruff_failure_is_a_fix_then_a_push():
    plan = plan_repair("ruff found errors", check="Lint")
    assert plan["pattern"] == "ruff-fix"
    assert plan["resubmit"] == "push"


def test_a_short_clean_run_is_rerun_not_a_rebase():
    plan = plan_repair("the reader barely ran; the test proved nothing")
    assert plan["pattern"] == "short-run"
    assert plan["resubmit"] == "rerun"
    assert plan["commands"] == []


def test_an_unknown_failure_is_not_given_a_fake_repair():
    plan = plan_repair("Some never-before-seen exotic flake in the build")
    assert plan["pattern"] == "unknown"
    assert plan["escalate"] is True
    assert plan["resubmit"] == "none"


def test_a_third_mechanical_try_stops_even_when_the_log_is_known():
    plan = plan_repair("ruff found errors", check="Lint", attempts=3)
    assert plan["pattern"] == "try-cap"
    assert plan["escalate"] is True
    assert plan["resubmit"] == "none"
    assert plan["commands"] == []


def test_the_reviewer_is_not_the_model_that_wrote_the_change():
    picked = choose_reviewer("writer-model", ["writer-model", "review-model"])
    assert picked["ok"] is True
    assert picked["reviewer"] == "review-model"
    refused = choose_reviewer("writer-model", ["writer-model"])
    assert refused["ok"] is False
    assert refused["reviewer"] == ""


def test_the_repair_and_review_are_mcp_tools():
    registry = SkillRegistry()
    bind(registry)
    repair = registry.spec("release.ci_repair")
    review_spec = registry.spec("release.review")
    assert repair is not None and is_mcp_exposed(repair)
    assert review_spec is not None and is_mcp_exposed(review_spec)
    assert mcp_tool_name("release.ci_repair") == "axiom_release__ci_repair"
    assert mcp_tool_name("release.review") == "axiom_release__review"
    ctx = SkillContext(
        registry=registry,
        state_dir=Path("/tmp"),
        logger=logging.getLogger("ci-flow"),
    )
    planned = ci_repair({"log": "ruff found errors", "check": "Lint"}, ctx)
    assert planned.ok is True
    assert planned.value["resubmit"] == "push"
    picked = review(
        {"author_model": "writer-model", "models": "writer-model, review-model", "pull": "1316"},
        ctx,
    )
    assert picked.ok is True
    assert picked.value["reviewer"] == "review-model"
    assert picked.value["pull"] == "1316"
