# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Workflows start only on events that can change their answer (issue #2441, items 12 and 13).

The RIVET lander finishes a landing decision when CI completes, when a pull request
opens or becomes ready, and on its schedules. Another workflow finishing (Semgrep,
pip-audit, npm-audit, Loop guards) changes nothing it reads, so none may start it.

The dependency audits read lock and requirements files. A path filter on the two
audit workflows is safe because neither is a required check: main's ruleset requires
Lint, Unit Tests (3.13) and Guards only (read with `gh api repos/<repo>/rulesets`,
2026-11-25). A path-filtered required check never reports on a pull request that
skips it and blocks the merge. If an audit is ever made required, replace the filter
with an early success in the job, as ci.yml does for Changed Paths. The nightly
schedule keeps a newly published advisory from waiting on the next dependency edit.

Semgrep's main push is a full-tree scan and stays unfiltered, and the same scan runs nightly and
on demand (tests/test_semgrep_nightly_and_incident.py). Its pull request run scans only the files
the pull request changed (see tests/test_semgrep_pr_scan.py); a docs-only pull request runs the
secrets pack alone. A red push or nightly files one incident and reverts nothing: the auto-revert
listens to CI and Loop guards only. Semgrep is not a required check, so no path rule can block a
merge. The main run still scans the merged tree, so it is not a repeat of the pull request scan.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

REQUIRED_CHECKS = {"Lint", "Unit Tests (3.13)", "Guards"}


def _on(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    return data.get("on", data.get(True))


def _glob_matches(pattern: str, path: str) -> bool:
    """GitHub's path glob: `**` crosses directories, `*` does not."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.fullmatch(out, path) is not None


def _triggered(paths: list[str], changed: str) -> bool:
    return any(_glob_matches(p, changed) for p in paths)


def test_the_lander_starts_when_ci_completes_and_no_other_workflow_does():
    on = _on("rivet-lander.yml")
    assert on["workflow_run"]["workflows"] == ["CI"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert set(on) == {"pull_request", "schedule", "workflow_dispatch", "push", "workflow_run"}
    assert on["push"]["branches"] == ["main"]
    assert on["pull_request"]["types"] == ["opened", "synchronize", "reopened", "ready_for_review"]
    assert {entry["cron"] for entry in on["schedule"]} == {"17 */6 * * *", "7 * * * *"}


def test_the_lander_queues_completions_instead_of_cancelling_a_landing():
    job = yaml.safe_load((WORKFLOWS / "rivet-lander.yml").read_text())["jobs"]["land"]
    concurrency = job["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_the_auto_revert_still_hears_ci_and_loop_guards():
    on = _on("loop-auto-revert.yml")
    assert on["workflow_run"]["workflows"] == ["CI", "Loop guards"]
    assert on["workflow_run"]["branches"] == ["main"]


def test_audits_run_only_when_a_dependency_file_changes_and_nightly():
    for name, own_file in (("pip-audit.yml", "pip-audit"), ("npm-audit.yml", "npm-audit")):
        on = _on(name)
        assert {"pull_request", "push", "schedule", "workflow_dispatch"} <= set(on)
        assert on["pull_request"]["branches"] == ["main"]
        assert on["push"]["branches"] == ["main"]
        assert on["pull_request"]["paths"] == on["push"]["paths"]
        assert f".github/workflows/{own_file}.yml" in on["pull_request"]["paths"]
        assert len(on["schedule"]) == 1 and on["schedule"][0]["cron"]


def test_the_python_audit_watches_what_it_audits_and_nothing_else():
    paths = _on("pip-audit.yml")["pull_request"]["paths"]
    for changed in (
        "pyproject.toml",
        "packages/axiom-tests/pyproject.toml",
        "packages/axiom-ext-data-platform/pyproject.toml",
        "requirements.txt",
        "packages/axiom-tests/requirements-dev.txt",
        "uv.lock",
        ".github/scripts/pip_audit_gate.py",
        ".github/workflows/pip-audit.yml",
    ):
        assert _triggered(paths, changed), changed
    for changed in (
        "src/axiom/chat/agent.py",
        "tests/test_ci_trigger_hygiene.py",
        "docs/adrs/adr-001.md",
        "src/axiom/extensions/builtins/webgate/frontend/package-lock.json",
        ".github/workflows/ci.yml",
    ):
        assert not _triggered(paths, changed), changed


def test_the_javascript_audit_watches_every_lock_and_manifest():
    paths = _on("npm-audit.yml")["pull_request"]["paths"]
    for changed in (
        "package-lock.json",
        "src/axiom/extensions/builtins/webgate/frontend/package-lock.json",
        "src/axiom/extensions/builtins/receipts/webui/package.json",
        ".github/scripts/npm_audit_gate.py",
        ".github/workflows/npm-audit.yml",
    ):
        assert _triggered(paths, changed), changed
    for changed in ("src/axiom/chat/agent.py", "pyproject.toml", ".github/workflows/ci.yml"):
        assert not _triggered(paths, changed), changed


def test_semgrep_main_push_stays_a_full_scan_and_the_pull_request_run_is_diff_aware():
    on = _on("semgrep.yml")
    # Main: unfiltered, whole tree. Pull request: no `paths` allow-list that could hide a code
    # change, only an ignore list of non-code extensions.
    assert "paths" not in on["push"] and "paths-ignore" not in on["push"]
    assert "paths" not in on["pull_request"] and "paths-ignore" not in on["pull_request"]
    assert "--baseline-commit" not in (WORKFLOWS / "semgrep.yml").read_text()
    assert "semgrep_pr_targets.sh" in (WORKFLOWS / "semgrep.yml").read_text()


def test_no_path_filtered_workflow_is_a_required_check():
    """A required check that a path filter skips never reports and blocks the merge."""
    filtered = {"pip-audit", "npm-audit", "Semgrep"}
    assert not (filtered & REQUIRED_CHECKS)
