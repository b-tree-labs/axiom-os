# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The restart job for the ci:full label (#2441).

Run 45764596938 failed: the job has no checkout, so every `gh` call said "failed to
determine base repo: not a git repository", the CI run never stopped, and the job ended red.
It also restarted a run that had already read the label: the label was added two seconds
after the CI run was created, and CI's change job read it live seven seconds later.

Recorded data (gh api, read only): the label events of two pull requests and the change job of
CI run 45764588506 (steps with start and finish times).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "release" / "data"
WORKFLOW = ROOT / ".github" / "workflows" / "ci-full-label.yml"

sys.path.insert(0, str(ROOT / "scripts"))
import ci_full_label_restart as restart  # noqa: E402


def _job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["restart"]


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


# --- the gh calls know their repository ---------------------------------------------------


def test_every_gh_call_names_its_repository_because_the_job_has_no_git_checkout():
    job = _job()
    has_checkout = any("checkout" in str(s.get("uses", "")) for s in job["steps"])
    if has_checkout:
        return
    if job.get("env", {}).get("GH_REPO") == "${{ github.repository }}":
        return
    for step in job["steps"]:
        for line in str(step.get("run", "")).splitlines():
            if re.search(r"\bgh (run|pr|issue|workflow)\b", line):
                assert " -R " in line or " --repo" in line, f"gh without a repo and without a checkout: {line.strip()}"


def test_the_job_sets_the_repo_for_gh_in_its_env():
    assert _job()["env"]["GH_REPO"] == "${{ github.repository }}"


def test_a_run_that_finished_or_was_superseded_is_a_notice_not_a_failure():
    text = WORKFLOW.read_text()
    notice, superseded, error = text.index("::notice::"), text.index("superseded"), text.find("::error::")
    assert notice < error or error == -1
    assert superseded < error or error == -1, "only a run that is still stuck and not superseded is an error"
    assert "did not stop" in text


def test_the_job_reads_the_decision_script_from_a_sparse_checkout():
    job = _job()
    checkout = next(s for s in job["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout["with"]["sparse-checkout"].strip() == "scripts/ci_full_label_restart.py"
    assert "ci_full_label_restart.py decide" in WORKFLOW.read_text()


# --- the skip decision, on recorded data --------------------------------------------------


def test_a_label_added_before_the_change_job_read_it_needs_no_restart():
    """#2497: label at 05:54:17, the change job's checkout finished 05:54:24 and it read the label next."""
    decision = restart.decide(_load("restart_2497_label_event.json"), _load("restart_2497_changes_job.json"))
    assert decision.restart is False
    assert "already read" in decision.reason


def test_the_label_ci_applies_itself_needs_no_restart():
    """#2499: the label came from the Actions bot, i.e. CI's own step in the run that read the paths."""
    decision = restart.decide(_load("restart_2499_label_event.json"), _load("restart_2497_changes_job.json"))
    assert decision.restart is False
    assert "CI applied" in decision.reason


def test_a_label_added_after_the_change_job_read_labels_restarts():
    event = _load("restart_2497_label_event.json")
    event["created_at"] = "2026-11-26T06:30:00Z"
    assert restart.decide(event, _load("restart_2497_changes_job.json")).restart is True


def test_an_unreadable_run_restarts_rather_than_guessing():
    event = _load("restart_2497_label_event.json")
    assert restart.decide(event, None).restart is True
    assert restart.decide(event, {"steps": []}).restart is True


def test_an_unlabel_always_restarts():
    event = _load("restart_2497_label_event.json")
    event["event"] = "unlabeled"
    assert restart.decide(event, _load("restart_2497_changes_job.json")).restart is True


def test_the_script_prints_the_decision(tmp_path):
    import subprocess

    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "ci_full_label_restart.py"),
            "decide",
            "--label-event",
            str(DATA / "restart_2497_label_event.json"),
            "--changes-job",
            str(DATA / "restart_2497_changes_job.json"),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.splitlines()[0] == "restart=false"
