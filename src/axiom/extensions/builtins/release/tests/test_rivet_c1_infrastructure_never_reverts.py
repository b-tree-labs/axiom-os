# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1 reland: auto-revert never reverts without a named failing test that touches the merge.

Auto-revert reverted #2436 (0d27ff808) for main run 45692341243. That run failed on infrastructure:
``Integration Tests`` died in "Install system dependencies" (apt-get exit 100, a dpkg lock held by
unattended-upgrades) and ``Install-Path Tests`` failed in the opt-in e2e because a docker build
could not load metadata for ``python:3.12-slim``. The revert's evidence read "no test ids in the
failed job's log", and the old rule judged a failure with no test ids "as before": real.

Acceptance ids:

  C1-RELAND-A  A red run with no named failing test (a setup or install step, docker, network or
               apt, a missing log) is "no test result": the failed jobs are re-run once, and it is
               never reverted.
  C1-RELAND-B  Still red on the re-run: ONE incident says main is red without a named failing
               test, which jobs and steps failed and why, and why no revert was made.
  C1-RELAND-C  A named failing test that does not touch the merge is still not reverted (the
               recorded harness failure is one).
  C1-RELAND-D  The reading of a failed attempt names the jobs, the failed step and the platform's
               own message.

Recorded with read-only ``gh api``: the run, its jobs and steps, both failed jobs' annotations and
log tails, and the merge commit (``fixtures/rivet_c1/*45692341243*``). One synthetic piece: a green
run of the commit before the merge, which the bisect reads.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.release import auto_revert as AR
from axiom.extensions.builtins.release.attempts import read_jobs
from axiom.extensions.builtins.release.tests.test_loop_s4_a_commit_that_breaks_main_is_reverted import (
    CONFIG,
    REPO,
    Forge,
    Git,
    _run,
)

FIX = Path(__file__).parent / "fixtures" / "rivet_c1"
RUN = 45692341243
MERGE = "84aee9d08631593e0aab06549b179feff7fb6d7a"
APT, DOCKER = 139480870016, 139480870026


def _json(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Recorded(Forge):
    def __init__(self, *, attempt: int = 1, logs: bool = True) -> None:
        recorded = _json(f"run_{RUN}.json")
        recorded["run_attempt"] = attempt
        super().__init__([_run(1, "g0", "success", minute=0), recorded], head=MERGE)
        commit = _json("commit_84aee9d.json")
        self.commits[MERGE] = {"sha": MERGE, "message": commit["message"], "author_login": commit["author"], "parents": ["g0"]}
        self.order = ["root", "g0", MERGE]
        self.files[MERGE] = commit["files"]
        self.recorded_jobs = _json(f"run_{RUN}.jobs.json")["jobs"]
        self.tails = {
            APT: (FIX / f"run_{RUN}_job_{APT}.log.tail.txt").read_text(encoding="utf-8"),
            DOCKER: (FIX / f"run_{RUN}_job_{DOCKER}.log.tail.txt").read_text(encoding="utf-8"),
        }
        self.with_logs = logs
        self.notes = {j: [a["message"] for a in _json(f"run_{RUN}_job_{j}.annotations.json")] for j in (APT, DOCKER)}

    def jobs(self, repo, run_id):
        return self.recorded_jobs

    def attempt_jobs(self, repo, run_id, attempt):
        return self.recorded_jobs

    def job_log(self, repo, job_id):
        return self.tails.get(int(job_id), "") if self.with_logs else ""

    def annotations(self, repo, job_id):
        return self.notes.get(int(job_id), [])


# --- A: no test result is never reverted ------------------------------------------------------


def test_c1_reland_a_the_recorded_red_with_no_test_ids_is_re_run_not_reverted():
    forge = Recorded(logs=False)  # the log gave no test id: what the revert's evidence said
    decision = AR.decide(forge, CONFIG, REPO, MERGE)
    assert decision.action == AR.RERUN, decision.reasons
    said = " ".join(decision.reasons)
    assert "without a named failing test" in said and "not counted as a failure" in said
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert forge.reruns == [RUN] and git.calls == []


def test_c1_reland_a_the_recorded_run_with_its_real_log_tails_is_not_reverted_either():
    forge = Recorded(logs=True)
    assert AR.decide(forge, CONFIG, REPO, MERGE).action != AR.REVERT


def test_c1_reland_a_a_setup_step_failure_with_no_log_at_all_is_not_a_failing_test():
    forge = Recorded(logs=False)
    forge.notes = {}
    assert AR.decide(forge, CONFIG, REPO, MERGE).action == AR.RERUN


# --- B: the one incident ------------------------------------------------------------------------


def test_c1_reland_b_still_red_on_the_re_run_opens_one_incident_naming_jobs_steps_and_why_no_revert():
    forge = Recorded(attempt=2, logs=False)
    decision = AR.decide(forge, CONFIG, REPO, MERGE)
    assert decision.action == AR.ASK, decision.reasons
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert git.calls == []
    assert len(forge.issues) == 1
    body = forge.issues[0]["body"]
    assert "red without a named failing test" in body
    assert "Integration Tests" in body and "Install system dependencies" in body
    assert "Install-Path Tests" in body and "Run opt-in federation + classroom e2e" in body
    assert "exit code 100" in body
    assert "nothing is reverted" in body
    assert "touches this merge" in body or "touch this merge" in body
    again = AR.decide(forge, CONFIG, REPO, MERGE)
    AR.carry_out(forge, CONFIG, again, git=git, act=True, sender=lambda r, a: None)
    assert len(forge.issues) == 1 and len(forge.comments) == 1, "the second pass comments on the same incident"


# --- C: a named failing test that the merge does not touch -----------------------------------------


def test_c1_reland_c_the_recorded_harness_test_is_named_and_not_blamed():
    forge = Recorded(attempt=2, logs=True)
    decision = AR.decide(forge, CONFIG, REPO, MERGE)
    assert decision.action == AR.ASK, decision.reasons


# --- D: the reading -----------------------------------------------------------------------------


def test_c1_reland_d_a_failed_attempt_with_no_test_names_its_jobs_steps_and_platform_message():
    jobs = [j for j in _json(f"run_{RUN}.jobs.json")["jobs"] if j["conclusion"] == "failure"]
    reading = read_jobs(lambda job: "", jobs, notes=lambda job: ["Process completed with exit code 100."] if job["id"] == APT else [])
    assert reading.kind == "other"
    assert "Integration Tests" in reading.detail and "Install system dependencies" in reading.detail
    assert "Install-Path Tests" in reading.detail and "Run opt-in federation + classroom e2e" in reading.detail
    assert "exit code 100" in reading.detail
