# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Auto-revert names the failing tests of a sharded run, from every failed shard (#2441).

Main's unit suite runs as ``Unit Shard (N)`` jobs plus an aggregator ("Fail when a shard failed")
that carries no test ids. The blame step read the failed step of each job but not the shard's pytest
summary as a named, attributed result, so a real red on main could not be told from a setup failure.
This reads the failing test ids from every failed job, says which shard named each, and judges a
guard that scans many files by the files its own failure names.

Recorded with read-only ``gh api`` (``fixtures/rivet_shards``): runs 45745563783 (two guard failures in
two shards) and 45723309744 (failures in three shards), the shard logs' short test summaries, the jobs,
and the commits' file lists. Two pieces are synthetic and said so: the re-run of each run is the same
recorded jobs, and "the guard failures belong to the agreement change" re-attributes the recorded run to
that merge (661241c90), because the recorded run's own head (63a071485) is the audit change.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.release import attempts as run_attempts
from axiom.extensions.builtins.release import auto_revert as AR
from axiom.extensions.builtins.release.blame import blame_tests
from axiom.extensions.builtins.release.ci_health.client import CIClientError, GhClient
from axiom.extensions.builtins.release.tests.test_loop_s4_a_commit_that_breaks_main_is_reverted import (
    CONFIG,
    REPO,
    Forge,
    Git,
    _run,
)

FIX = Path(__file__).parent / "fixtures" / "rivet_shards"
GUARD_RUN, LEAK_RUN = 45745563783, 45723309744
AUDIT, RIVET_LAND = "63a071485a884bab02590f0ac9ebdafad286407b", "491b5258296d094d3af8e31c0ba8228a3ffba7b9"
STATE_TEST = "tests/infra/test_state_dir_has_one_spelling.py::test_no_module_spells_the_home_state_folder_itself"
BRAND_TEST = "tests/infra/test_cli_name_branding.py::test_no_new_hardcoded_axi_in_operator_facing_text"


def _json(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _commit(prefix: str) -> dict:
    path = next(FIX.glob(f"commit_{prefix}*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


AGREEMENT = _commit("661241c90")["sha"]


class Sharded(Forge):
    """A forge serving a recorded sharded run. ``merge`` is the commit the run is judged against."""

    def __init__(self, run_id: int, *, merge: str, attempt: int = 1, rerun_jobs: bool = True, logs: bool = True) -> None:
        recorded = _json(f"run_{run_id}.json")
        row = _run(1, "g0", "success", minute=0)
        red = _run(run_id, merge, "failure", minute=5, attempt=attempt)
        super().__init__([row, red], head=merge)
        info = _commit(merge[:9])
        self.commits[merge] = {"sha": merge, "message": info["message"], "author_login": info["author"], "parents": ["g0"]}
        self.order = ["root", "g0", merge]
        self.files[merge] = info["files"]
        self.run_id = run_id
        self.recorded_jobs = _json(f"run_{run_id}.attempt1.jobs.json")["jobs"]
        self.rerun_jobs = rerun_jobs
        self.with_logs = logs
        self.excerpts = {
            int(p.name.split("_job_")[1].split(".")[0]): p.read_text(encoding="utf-8")
            for p in FIX.glob(f"run_{run_id}_job_*.log.excerpt.txt")
        }
        assert recorded["id"] == run_id

    def jobs(self, repo, run_id):
        return self.recorded_jobs if self.rerun_jobs or int(self.revert_attempt) == 1 else []

    revert_attempt = 1

    def attempt_jobs(self, repo, run_id, attempt):
        return self.recorded_jobs

    def job_log(self, repo, job_id):
        return self.excerpts.get(int(job_id), "") if self.with_logs else ""


# --- the reading: every failed shard, and which shard named each test -------------------------------


def test_every_failed_shard_is_read_and_each_test_says_which_shard_named_it():
    forge = Sharded(GUARD_RUN, merge=AUDIT)
    reading = run_attempts.read_current(forge, REPO, GUARD_RUN)
    assert reading.kind == run_attempts.TESTS
    assert set(reading.tests) == {STATE_TEST, BRAND_TEST}
    assert dict(reading.sources) == {STATE_TEST: ("Unit Shard (3)",), BRAND_TEST: ("Unit Shard (2)",)}
    said = run_attempts.describe(reading, "the run")
    assert "Unit Shard (3)" in said and "Unit Shard (2)" in said


def test_a_failure_in_three_shards_names_all_three():
    forge = Sharded(LEAK_RUN, merge=RIVET_LAND)
    reading = run_attempts.read_current(forge, REPO, LEAK_RUN)
    shards = {shard for names in dict(reading.sources).values() for shard in names}
    assert shards == {"Unit Shard (1)", "Unit Shard (2)", "Unit Shard (3)"}
    assert len(reading.tests) == 5 + 15 + 27


def test_the_aggregator_alone_names_no_test_and_is_not_a_result():
    jobs = [j for j in _json(f"run_{GUARD_RUN}.attempt1.jobs.json")["jobs"] if j["name"] == "Unit Tests (3.13)"]
    excerpt = (FIX / "run_45745563783_job_139668493302.log.excerpt.txt").read_text(encoding="utf-8")
    reading = run_attempts.read_jobs(lambda job: excerpt, jobs)
    assert reading.kind == run_attempts.OTHER and not reading.tests
    assert "Fail when a shard failed" in reading.detail


def test_the_aggregator_adds_nothing_to_what_the_shards_named():
    forge = Sharded(GUARD_RUN, merge=AUDIT)
    reading = run_attempts.read_current(forge, REPO, GUARD_RUN)
    assert all("Unit Tests (3.13)" not in names for names in dict(reading.sources).values())


# --- the decision on the recorded runs ----------------------------------------------------------------


def test_run_45745563783_first_pass_names_both_guards_and_their_shards_before_it_re_runs():
    forge = Sharded(GUARD_RUN, merge=AUDIT)
    decision = AR.decide(forge, CONFIG, REPO, AUDIT)
    assert decision.action == AR.RERUN, decision.reasons
    said = " ".join(decision.reasons)
    assert "without a named failing test" not in said
    assert "test_state_dir_has_one_spelling" in said and "Unit Shard (3)" in said
    assert "test_cli_name_branding" in said and "Unit Shard (2)" in said


def test_run_45745563783_after_the_re_run_does_not_blame_the_audit_change():
    forge = Sharded(GUARD_RUN, merge=AUDIT, attempt=2)
    decision = AR.decide(forge, CONFIG, REPO, AUDIT)
    assert decision.action == AR.ASK, decision.reasons
    assert decision.blamed == []
    assert {c["test"] for c in decision.cleared} == {STATE_TEST, BRAND_TEST}
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert git.calls == []
    body = forge.issues[0]["body"]
    assert "Unit Shard (3)" in body and "Unit Shard (2)" in body
    assert "the same test failed in the first run and in the re-run" in body


def test_run_45745563783_judged_against_the_agreement_change_names_it_and_would_revert():
    # Synthetic attribution, as said above: the recorded failures, the agreement merge's recorded files.
    forge = Sharded(GUARD_RUN, merge=AGREEMENT, attempt=2)
    decision = AR.decide(forge, CONFIG, REPO, AGREEMENT)
    assert decision.action == AR.REVERT, decision.reasons
    assert {b["test"] for b in decision.blamed} == {STATE_TEST, BRAND_TEST}
    why = " ".join(b["why"] for b in decision.blamed)
    assert "agreement/paths.py" in why and "agreement/cli.py" in why
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert len(git.calls) == 1 and git.calls[0]["sha"] == AGREEMENT
    assert "Unit Shard (3)" in forge.issues[0]["body"] and "Unit Shard (2)" in forge.issues[0]["body"]


def test_run_45723309744_failures_in_three_shards_that_the_merge_does_not_touch_are_not_reverted():
    forge = Sharded(LEAK_RUN, merge=RIVET_LAND, attempt=2)
    decision = AR.decide(forge, CONFIG, REPO, RIVET_LAND)
    assert decision.action == AR.ASK, decision.reasons
    assert decision.blamed == []
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert git.calls == []
    body = forge.issues[0]["body"]
    assert all(f"Unit Shard ({n})" in body for n in (1, 2, 3))


def test_a_re_run_that_produced_no_jobs_is_never_a_revert_even_with_named_first_failures():
    forge = Sharded(GUARD_RUN, merge=AGREEMENT, attempt=2, rerun_jobs=False)
    forge.revert_attempt = 2
    decision = AR.decide(forge, CONFIG, REPO, AGREEMENT)
    assert decision.action == AR.ASK, decision.reasons
    assert Git().calls == []


def test_infrastructure_red_with_no_test_ids_is_still_never_a_revert_in_the_sharded_era():
    from axiom.extensions.builtins.release.tests.test_rivet_c1_infrastructure_never_reverts import Recorded

    for attempt in (1, 2):
        forge = Recorded(attempt=attempt, logs=False)
        decision = AR.decide(forge, CONFIG, REPO, "84aee9d08631593e0aab06549b179feff7fb6d7a")
        assert decision.action in (AR.RERUN, AR.ASK), decision.reasons


# --- blame: a guard that scans many files ------------------------------------------------------------------


def test_a_scan_guard_is_blamed_for_the_file_its_own_failure_names_when_the_merge_changed_it():
    named = {STATE_TEST: ["axiom/extensions/builtins/release/agreement/paths.py"]}
    result = blame_tests([STATE_TEST], ["src/axiom/extensions/builtins/release/agreement/paths.py"], named=named)
    assert [b.test for b in result.blamed] == [STATE_TEST]
    assert "agreement/paths.py" in result.blamed[0].why


def test_a_scan_guard_is_not_blamed_for_a_merge_that_changed_other_files_it_scans():
    named = {STATE_TEST: ["axiom/extensions/builtins/release/agreement/paths.py"]}
    changed = ["src/axiom/infra/state.py", "src/axiom/infra/audit_log.py"]
    assert blame_tests([STATE_TEST], changed, named=named).blamed == ()


def test_a_scan_guard_whose_failure_names_no_file_is_never_blamed_for_adding_a_file_it_scans():
    # Decision: scanning a file is not touching it. A repo-wide scan reads every file, so it would blame every
    # merge that adds one. Only a file the failure itself names counts; with none named, nothing is blamed.
    result = blame_tests([STATE_TEST], ["src/axiom/brand_new_module.py"], named={STATE_TEST: []})
    assert result.blamed == ()


def test_a_named_path_matches_on_a_directory_boundary_only():
    named = {BRAND_TEST: ["extensions/builtins/release/agreement/cli.py"]}
    assert blame_tests([BRAND_TEST], ["src/axiom/extensions/builtins/release/agreement/xcli.py"], named=named).blamed == ()
    assert blame_tests([BRAND_TEST], ["src/axiom/extensions/builtins/release/agreement/cli.py"], named=named).blamed


# --- reading the log: a log that is not there yet is retried, and said --------------------------------------


def test_a_log_that_is_not_there_yet_is_retried_before_the_job_counts_as_unreadable():
    calls: list[list[str]] = []
    pauses: list[float] = []

    def runner(args):
        calls.append(list(args))
        if len(calls) < 3:
            raise CIClientError("gh api: HTTP 404")
        return "FAILED tests/test_a.py::test_x - assert 0\n"

    client = GhClient(runner=runner, sleep=pauses.append)
    assert "test_x" in client.job_log(REPO, 7)
    assert len(calls) == 3 and len(pauses) == 2


def test_a_log_that_never_arrives_is_said_in_the_reading_not_called_a_failed_step():
    jobs = [{"id": 1, "name": "Unit Shard (1)", "conclusion": "failure", "steps": [{"name": "Run unit tests", "conclusion": "failure"}]}]
    reading = run_attempts.read_jobs(lambda job: "", jobs)
    assert reading.kind == run_attempts.OTHER
    assert "log could not be read" in reading.detail
