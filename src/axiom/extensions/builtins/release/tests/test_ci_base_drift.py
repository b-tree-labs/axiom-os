# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Rivet handles a red check the pull request did not cause.

Acceptance rows BD-01 to BD-06. The motivating case: a mobile pull request
that changed no package file went red because an upstream release changed
the framework's expected-version table after the base branch was last
green. Every mobile pull request fails until the base is bumped, and the
dependencies belong to another engineer. The right plan fixes the base
once, under CI's toolchain, on the blocked pull request's own branch,
verifies it, pushes it, and tells the owner afterwards. Other blocked pull
requests wait for that one repair instead of repeating it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.extensions.builtins.release.ci_drift import (
    attribute_failure,
    codeowners_for,
    decide_landing,
    detect_toolchain,
    parse_numstat,
    version_changes,
)
from axiom.extensions.builtins.release.ci_flow import REPAIR_TRY_CAP, plan_repair
from axiom.extensions.builtins.release.repair_catalog import (
    CATALOG,
    draft_entry,
    match_entry,
)
from axiom.extensions.builtins.release.skills import bind, ci_repair
from axiom.infra.skills import SkillContext, SkillRegistry

EXPO_LOG = """\
##[error]Expo SDK major/minor mismatch (patch drift is ignored):
  expo-web-browser 58.0.7 - expected version: ~58.1.0
##[warning]expo 58.0.22 vs expected ~58.0.26 (patch-only; not failing CI)
Your project may not work correctly until you install the expected versions of the packages.
Found outdated dependencies
##[error]Process completed with exit code 1.
"""

MOBILE_WORKFLOW = """\
name: Mobile
on: pull_request
jobs:
  mobile-check:
    name: Mobile checks
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: apps/mobile
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: apps/mobile/package-lock.json
      - run: npm ci
      - run: npx expo install --check
"""

CODEOWNERS = """\
# Default owners
*                 @example-org/platform
/apps/mobile/     @mobile-owner
"""

PINNED_DIFF = "1\t1\tapps/mobile/package.json\n14\t12\tapps/mobile/package-lock.json\n"
CHURNED_DIFF = "1\t1\tapps/mobile/package.json\n2400\t1900\tapps/mobile/package-lock.json\n"

INCIDENT_FACTS = {
    "pr_files": ["apps/mobile/src/screens/Settings.tsx", "apps/mobile/src/i18n/en.json"],
    "check_globs": ["apps/mobile/**"],
    "base_branch": "main",
    "base_result": "success",
    "base_result_at": "2026-11-10T15:00:00Z",
    "upstream_release_at": "2026-11-12T18:00:00Z",
    "workflow": MOBILE_WORKFLOW,
    "job": "mobile-check",
    "diff_stats": PINNED_DIFF,
    "codeowners": CODEOWNERS,
    "actor": "rivet-bot",
}


# --- BD-01: attribution -----------------------------------------------------


def test_bd01_a_pr_that_changed_no_package_file_and_a_stale_green_base_is_base_drift():
    verdict = attribute_failure(
        entry_inputs=("**/package.json", "**/package-lock.json"),
        pr_files=INCIDENT_FACTS["pr_files"],
        check_globs=INCIDENT_FACTS["check_globs"],
        base_result="success",
        base_result_at="2026-11-10T15:00:00Z",
        upstream_release_at="2026-11-12T18:00:00Z",
    )
    assert verdict["verdict"] == "base_drift"
    assert any("upstream" in line for line in verdict["evidence"])
    assert any("package" in line for line in verdict["evidence"])


def test_bd01_a_pr_that_changed_the_package_file_caused_it():
    verdict = attribute_failure(
        entry_inputs=("**/package.json",),
        pr_files=["apps/mobile/package.json"],
        base_result="success",
        base_result_at="2026-11-10T15:00:00Z",
    )
    assert verdict["verdict"] == "caused_by_pr"


def test_bd01_the_same_check_red_on_base_is_base_drift():
    verdict = attribute_failure(pr_files=["README.md"], base_result="failure")
    assert verdict["verdict"] == "base_drift"


def test_bd01_no_facts_is_undetermined_and_names_the_fact_that_would_decide():
    verdict = attribute_failure()
    assert verdict["verdict"] == "undetermined"
    assert verdict["next_fact"]


def test_bd01_a_recent_green_base_with_untouched_inputs_is_undetermined():
    verdict = attribute_failure(
        pr_files=["docs/readme.md"],
        check_globs=["apps/mobile/**"],
        base_result="success",
        base_result_at="2026-11-12T18:00:00Z",
    )
    assert verdict["verdict"] == "undetermined"
    assert "base" in verdict["next_fact"]


# --- BD-02: the catalog -------------------------------------------------------


def test_bd02_every_entry_declares_the_full_shape():
    ids = [entry.id for entry in CATALOG]
    assert len(ids) == len(set(ids))
    for entry in CATALOG:
        assert entry.signatures, entry.id
        assert entry.default_attribution in {"caused_by_pr", "base_drift", "undetermined"}
        assert entry.resubmit in {"push", "rerun", "none", "after-base-fix"}
        assert entry.status == "active"


def test_bd02_the_legacy_patterns_still_lead_the_catalog_in_order():
    assert [entry.id for entry in CATALOG[:5]] == [
        "context-sync",
        "ruff-fix",
        "short-run",
        "stray-root",
        "customer-name",
    ]


def test_bd02_the_legacy_plans_are_byte_for_byte_unchanged():
    assert plan_repair("context files drifted", "") == {
        "ok": True,
        "pattern": "context-sync",
        "detail": "The generated context files drifted from AGENTS.md.",
        "commands": ["axi context sync"],
        "resubmit": "push",
        "escalate": False,
    }
    assert plan_repair("ruff found errors", "Lint")["commands"] == [
        "ruff check src tests conftest.py --select E,F,W --ignore E501 --fix"
    ]
    assert plan_repair("unexpected root dir") == {
        "ok": True,
        "pattern": "stray-root",
        "detail": "A directory at the repo root is not part of the tree. It is not deleted from here.",
        "commands": [],
        "resubmit": "none",
        "escalate": False,
    }
    assert plan_repair("forbidden term")["escalate"] is False
    assert plan_repair("weird") == {
        "ok": True,
        "pattern": "unknown",
        "detail": "No mechanical repair is known for this failure.",
        "commands": [],
        "resubmit": "none",
        "escalate": True,
    }
    assert REPAIR_TRY_CAP == 3


def test_bd02_each_drift_family_is_recognised():
    cases = {
        "expo-expected-version": EXPO_LOG,
        "npm-lockfile-out-of-sync": (
            "npm ERR! `npm ci` can only install packages when your package.json and "
            "package-lock.json are in sync.\nnpm ERR! Invalid: lock file's left-pad@1.0.0 "
            "does not satisfy left-pad@1.3.0"
        ),
        "pip-resolver-conflict": (
            "ERROR: Cannot install pkg-a==1.0 and pkg-b==2.0 because these package "
            "versions have conflicting dependencies.\nERROR: ResolutionImpossible"
        ),
        "action-runtime-deprecated": (
            "##[error]Node.js 16 actions are deprecated. Please update the following "
            "actions to use Node.js 20: actions/checkout@v3"
        ),
        "toolchain-version-not-found": (
            "##[error]The version '3.99' with architecture 'x64' was not found for Ubuntu 24.04."
        ),
    }
    for expected, log in cases.items():
        entry = match_entry(log)
        assert entry is not None and entry.id == expected, (expected, entry)


def test_bd02_the_expo_entry_pins_its_repair_and_its_verify():
    entry = match_entry(EXPO_LOG)
    assert entry is not None
    assert entry.commands == ("npx expo install --fix",)
    assert entry.verify == "npx expo install --check"
    assert entry.toolchain == "node"
    assert entry.default_attribution == "base_drift"
    assert entry.churn_budget is not None and entry.churn_budget.max_lines > 0


# --- BD-03: toolchain -----------------------------------------------------------


def test_bd03_the_toolchain_comes_from_the_workflow_not_this_machine():
    chain = detect_toolchain(MOBILE_WORKFLOW, "mobile-check")
    assert chain["found"] is True
    assert chain["node_version"] == "20"
    assert chain["npm_version"] == "10"
    assert chain["package_manager"] == "npm"
    assert chain["working_directory"] == "apps/mobile"
    assert chain["image"] == "node:20"


def test_bd03_the_job_is_found_by_its_display_name_too():
    assert detect_toolchain(MOBILE_WORKFLOW, "Mobile checks")["node_version"] == "20"


def test_bd03_a_python_job_and_an_explicit_npm_pin():
    workflow = """\
jobs:
  test:
    strategy:
      matrix:
        py: ["3.12"]
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.py }}
      - uses: actions/setup-node@v4
        with:
          node-version: '18.x'
      - run: npm install -g npm@9.9.3
      - run: pip install -r requirements.txt
"""
    chain = detect_toolchain(workflow, "test")
    assert chain["python_version"] == "3.12"
    assert chain["node_version"] == "18"
    assert chain["npm_version"] == "9.9.3"


def test_bd03_no_workflow_is_not_found():
    assert detect_toolchain("", "x")["found"] is False


# --- BD-04: landing guard: verify decides, ownership is a note --------------------


def test_bd04_numstat_is_summed():
    stats = parse_numstat(CHURNED_DIFF)
    assert stats["files"] == 2
    assert stats["lines"] == 4302


def test_bd04_codeowners_last_match_wins():
    assert codeowners_for("apps/mobile/package.json", CODEOWNERS) == ["@mobile-owner"]
    assert codeowners_for("src/app.py", CODEOWNERS) == ["@example-org/platform"]


def test_bd04_someone_elses_paths_are_pushed_and_the_owner_is_told():
    entry = match_entry(EXPO_LOG)
    guard = decide_landing(
        diff_stats=PINNED_DIFF,
        verify_result="pass",
        budget=entry.churn_budget,
        actor="rivet-bot",
        codeowners=CODEOWNERS,
    )
    assert guard["decision"] == "push"
    assert guard["notify"] == ["@mobile-owner"]


def test_bd04_churn_alone_does_not_stop_a_verified_repair():
    entry = match_entry(EXPO_LOG)
    guard = decide_landing(
        diff_stats=CHURNED_DIFF, verify_result="pass", budget=entry.churn_budget, actor="x"
    )
    assert guard["decision"] == "push"
    assert guard["churn"]["over_budget"] is True
    assert guard["churn"]["lines"] == 4302


def test_bd04_churn_with_a_failing_verify_escalates():
    entry = match_entry(EXPO_LOG)
    guard = decide_landing(
        diff_stats=CHURNED_DIFF, verify_result="fail", budget=entry.churn_budget, actor="x"
    )
    assert guard["decision"] == "escalate"
    assert any("verify failed" in reason and "budget" in reason for reason in guard["reasons"])


def test_bd04_recent_authors_stand_in_for_codeowners():
    guard = decide_landing(
        diff_stats=PINNED_DIFF,
        verify_result="pass",
        actor="rivet-bot",
        recent_authors={"apps/mobile/package.json": ["mobile-dev"]},
    )
    assert guard["decision"] == "push"
    assert guard["notify"] == ["mobile-dev"]


def test_bd04_a_sensitive_path_escalates():
    guard = decide_landing(
        diff_stats="3\t1\t.github/workflows/ci.yml\n", verify_result="pass", actor="x"
    )
    assert guard["decision"] == "escalate"


def test_bd04_an_empty_diff_stops_and_no_verify_yet_is_pending():
    assert decide_landing(diff_stats="", actor="x")["decision"] == "stop"
    assert decide_landing(diff_stats=PINNED_DIFF, actor="x")["decision"] == "pending_verify"


def test_bd04_a_major_version_change_is_found_in_the_log():
    changes = version_changes(EXPO_LOG)
    assert changes == [
        {
            "package": "expo-web-browser",
            "current": "58.0.7",
            "expected": "58.1.0",
            "major_change": False,
        }
    ]
    major = version_changes("  some-lib 57.2.0 - expected version: ~58.0.0\n")
    assert major[0]["major_change"] is True
    assert version_changes("  zero-lib 0.4.1 - expected version: ^0.5.0")[0]["major_change"] is True


# --- BD-05: learning hook ----------------------------------------------------------


def test_bd05_a_resolved_unknown_drafts_an_entry_that_is_not_active():
    log = "##[error]Widget cache schema 7 is older than required 9\n##[error]Process completed with exit code 1."
    draft = draft_entry(log, ["make widget-cache-refresh"], verify="make widget-check")
    assert draft.status == "needs_review"
    assert draft.commands == ("make widget-cache-refresh",)
    assert any("Widget cache schema" in sig for sig in draft.signatures)
    assert not any("exit code" in sig for sig in draft.signatures)
    # It matches its own log, but plan_repair never uses an entry under review.
    assert draft.matches(log)
    assert plan_repair(log, catalog=[draft])["pattern"] == "unknown"
    assert draft.to_dict()["status"] == "needs_review"


# --- BD-06: the plan the tool returns ---------------------------------------------


def test_bd06_the_incident_is_repaired_automatically_on_the_blocked_pr():
    facts = dict(INCIDENT_FACTS, pr="101", verify_result="pass")
    plan = plan_repair(EXPO_LOG, "mobile-check", facts=facts)
    assert plan["pattern"] == "expo-expected-version"
    assert plan["attribution"]["verdict"] == "base_drift"
    assert plan["fix_on"] == "pr_branch"
    assert plan["resubmit"] == "push"
    assert plan["escalate"] is False
    assert plan["commit_message"] == "rivet: base-drift repair — expo-expected-version"
    assert plan["toolchain"]["node_version"] == "20"
    assert plan["toolchain"]["npm_version"] == "10"
    assert all("node:20" in command for command in plan["commands"])
    assert all("/work/apps/mobile" in command for command in plan["commands"])
    assert "expo install --fix" in plan["commands"][0]
    assert "expo install --check" in plan["verify"]
    assert plan["guard"]["decision"] == "push"
    assert "not caused by this pull request" in plan["blocked_pr_comment"]
    note = plan["owner_note"]
    assert note.startswith("FYI @mobile-owner")
    assert "Node 20, npm 10" in note and "revert" in note
    assert "pull request 101" in note
    assert len(note.splitlines()) <= 8
    assert plan["after_landing"][1] == "git merge --no-edit origin/main"
    assert not any("update-branch" in step and "never" not in step for step in plan["after_landing"])


def test_bd06_a_second_blocked_pr_waits_for_the_first_repair():
    first = plan_repair(EXPO_LOG, "mobile-check", facts=dict(INCIDENT_FACTS, pr="101"))
    second = plan_repair(
        EXPO_LOG,
        "mobile-check",
        facts=dict(
            INCIDENT_FACTS,
            pr="102",
            in_flight=[{"key": first["drift_key"], "pr": "101"}],
        ),
    )
    assert second["waiting_on"] == "101"
    assert second["commands"] == []
    assert second["fix_on"] == "none"
    assert second["escalate"] is False
    assert any("git merge --no-edit origin/main" in step for step in second["steps"])


def test_bd06_a_branch_that_cannot_take_pushes_gets_one_auto_merging_repair_pr():
    plan = plan_repair(
        EXPO_LOG, "mobile-check", facts=dict(INCIDENT_FACTS, pr="103", branch_pushable=False)
    )
    assert plan["fix_on"] == "repair_pr"
    assert plan["repair_pr"]["auto_merge"] is True
    assert plan["repair_pr"]["close_if_superseded"] is True
    assert plan["repair_pr"]["one_per_key"] == plan["drift_key"]


def test_bd06_a_failing_verify_escalates():
    plan = plan_repair(EXPO_LOG, "mobile-check", facts=dict(INCIDENT_FACTS, verify_result="fail"))
    assert plan["escalate"] is True
    assert plan["resubmit"] == "none"
    assert any("verify failed" in reason for reason in plan["escalate_reasons"])


def test_bd06_a_major_version_bump_escalates():
    log = EXPO_LOG.replace("58.0.7 - expected version: ~58.1.0", "57.0.7 - expected version: ~58.1.0")
    plan = plan_repair(log, "mobile-check", facts=dict(INCIDENT_FACTS, verify_result="pass"))
    assert plan["escalate"] is True
    assert any("major version change" in reason for reason in plan["escalate_reasons"])


def test_bd06_caused_by_the_pr_is_an_ordinary_repair_on_that_pr():
    facts = dict(INCIDENT_FACTS, pr_files=["apps/mobile/package.json"], verify_result="pass")
    plan = plan_repair(EXPO_LOG, "mobile-check", facts=facts)
    assert plan["attribution"]["verdict"] == "caused_by_pr"
    assert plan["commit_message"] == "rivet: repair — expo-expected-version"
    assert "blocked_pr_comment" not in plan
    assert "owner_note" not in plan
    assert plan["escalate"] is False


def test_bd06_a_drift_entry_without_the_workflow_asks_for_it_and_runs_nothing():
    plan = plan_repair(EXPO_LOG)
    assert plan["pattern"] == "expo-expected-version"
    assert plan["commands"] == []
    assert plan["needs_facts"] == ["workflow", "job"]
    assert "toolchain" in plan["detail"].lower()


def test_bd06_the_try_cap_still_wins_over_facts():
    plan = plan_repair(EXPO_LOG, facts=INCIDENT_FACTS, attempts=REPAIR_TRY_CAP)
    assert plan["pattern"] == "try-cap"


def test_bd06_the_mcp_tool_takes_the_facts_as_strings():
    registry = SkillRegistry()
    bind(registry)
    spec = registry.spec("release.ci_repair")
    for name in ("pr_files", "base_result", "workflow", "diff_stats", "verify_result", "in_flight"):
        assert name in spec.inputs
    ctx = SkillContext(registry=registry, state_dir=Path("/tmp"), logger=logging.getLogger("bd"))
    params = dict(INCIDENT_FACTS)
    params["pr_files"] = "\n".join(INCIDENT_FACTS["pr_files"])
    params["check_globs"] = "apps/mobile/**"
    params["in_flight"] = '[{"key": "other:main:.", "pr": "7"}]'
    params.update({"log": EXPO_LOG, "check": "mobile-check", "pr": "101", "verify_result": "pass"})
    result = ci_repair(params, ctx)
    assert result.ok is True
    assert result.value["attribution"]["verdict"] == "base_drift"
    assert result.value["guard"]["decision"] == "push"
    assert result.value["escalate"] is False
    plain = ci_repair({"log": "ruff found errors", "check": "Lint"}, ctx)
    assert "attribution" not in plain.value


def test_bd06_the_lander_never_runs_a_drift_repair_without_the_toolchain():
    from axiom.extensions.builtins.release.fixes import MechanicalFixProvider
    from axiom.extensions.builtins.release.lander.model import OpenPull

    pull = OpenPull(
        number=1,
        title="mobile screen",
        head_sha="abc",
        head_ref="feat/x",
        files=("apps/mobile/src/screens/Settings.tsx",),
        checks="failing",
        failure_log=EXPO_LOG,
    )
    provider = MechanicalFixProvider()
    fix = provider.draft(provider.diagnose(pull), pull)
    assert fix.needs_human is True
    assert fix.commands == ()
