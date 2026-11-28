# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1, #2037 second case: a runner death plus one unrelated failure reverted a correct fix.

PR #2373 changed two functions in ``src/axiom/llm/anthropic_ingress.py``. The first main run on
its merge commit ended with no test result (the runner was killed, exit 137). The re-run failed
one video test in a file the commit did not touch (fixed later in #2415). Nothing of the changed
code failed in either run, and the two runs failed for different reasons. It was reverted.

Acceptance ids:

  C1-2037-G  Replay of the recorded case: no revert, and one ask that says the first run produced
             no test result and the re-run failed an unrelated test.
  C1-2037-H  A run with no test result (runner killed, job cancelled or timed out) is never
             counted as a failure of the commit: it is re-run, and only the re-run is judged.
  C1-2037-I  A revert needs the same failing test in the first run and in the re-run, and a
             failing test that touches the merge. Two different failures are not a repeat, and
             the incident says which it saw.

The data is recorded with read-only ``gh api`` calls: the two attempts' jobs, the exit-137
annotation, the log tails, the run and the merge commit (``fixtures/rivet_c1/*45465920350*``).
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
RUN = 45465920350
MERGE = "90d5127e57916e797517db7aa5fb6d7feffcf5be"
VIDEO = "src/axiom/extensions/builtins/video/tests/test_diagnose_names_the_exact_fix.py::test_the_last_rung_is_a_real_frame"


def _json(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Replay(Forge):
    """The recorded run of the merge commit, with attempts: jobs, annotations and log tails.

    One synthetic piece: a green run of the commit before it, which the forge adapter reads
    to bisect. Everything about the merge commit and its run is recorded.
    """

    def __init__(self, attempt: int) -> None:
        recorded = _json(f"run_{RUN}.json")
        recorded["run_attempt"] = attempt
        super().__init__([_run(1, "g0", "success", minute=0), recorded], head=MERGE)
        commit = _json("commit_90d5127.json")
        self.commits[MERGE] = {
            "sha": MERGE, "message": commit["message"], "author_login": commit["author"], "parents": ["g0"],
        }
        self.order = ["root", "g0", MERGE]
        self.files[MERGE] = commit["files"]
        self.attempt = attempt
        self.jobs_by_attempt = {n: _json(f"run_{RUN}_attempt{n}.jobs.json")["jobs"] for n in (1, 2)}
        self.logs = {
            138721184589: (FIX / f"run_{RUN}_attempt1.job_138721184589.log.tail.txt").read_text(encoding="utf-8"),
            138756703951: (FIX / f"run_{RUN}_attempt2.job_138756703951.log.failed.txt").read_text(encoding="utf-8"),
        }
        self.notes = {138721184589: [a["message"] for a in _json(f"run_{RUN}_attempt1.annotations.json")]}

    def jobs(self, repo, run_id):
        return self.jobs_by_attempt[self.attempt]

    def attempt_jobs(self, repo, run_id, attempt):
        return self.jobs_by_attempt[int(attempt)]

    def annotations(self, repo, job_id):
        return self.notes.get(int(job_id), [])


# --- G: the recorded case --------------------------------------------------------------------


def test_c1_2037_g_the_recorded_case_is_not_reverted_and_the_ask_says_what_each_run_did():
    forge = Replay(attempt=2)
    decision = AR.decide(forge, CONFIG, REPO, MERGE)
    assert decision.action == AR.ASK, decision.reasons
    said = " ".join(decision.reasons + decision.attempts)
    assert "first run produced no test result" in said
    assert "exit code 137" in said
    assert "re-run failed" in said and "test_the_last_rung_is_a_real_frame" in said
    assert [c["test"] for c in decision.cleared] == [VIDEO] and decision.blamed == []
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert git.calls == []
    assert len(forge.issues) == 1
    body = forge.issues[0]["body"]
    assert "first run produced no test result" in body and "test_the_last_rung_is_a_real_frame" in body
    assert "was **not** reverted" in body


# --- H: no test result is not a failure -----------------------------------------------------------


def test_c1_2037_h_a_first_run_with_no_test_result_is_re_run_never_reverted():
    forge = Replay(attempt=1)
    decision = AR.decide(forge, CONFIG, REPO, MERGE)
    assert decision.action == AR.RERUN, decision.reasons
    assert "no test result" in " ".join(decision.reasons) and "not counted as a failure" in " ".join(decision.reasons)
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert forge.reruns == [RUN] and git.calls == []


def test_c1_2037_h_two_runs_with_no_test_result_are_asked_about_not_reverted():
    forge = Replay(attempt=2)
    forge.jobs_by_attempt[2] = forge.jobs_by_attempt[1]  # the re-run was killed too
    decision = AR.decide(forge, CONFIG, REPO, MERGE)
    assert decision.action == AR.ASK
    assert "no test result" in " ".join(decision.reasons)


def test_c1_2037_h_what_counts_as_no_result_is_pure_and_narrow():
    killed = [{"id": 1, "name": "Unit Tests", "conclusion": "failure"}]
    reading = read_jobs(lambda job: "", killed, notes=lambda job: ["Process completed with exit code 137."])
    assert reading.kind == "no_result" and "exit code 137" in reading.detail
    timed_out = [{"id": 2, "name": "Unit Tests", "conclusion": "timed_out"}]
    assert read_jobs(lambda job: "", timed_out).kind == "no_result"
    cancelled = [{"id": 3, "name": "Unit Tests", "conclusion": "cancelled"}]
    assert read_jobs(lambda job: "", cancelled).kind == "no_result"
    lint = [{"id": 4, "name": "Lint", "conclusion": "failure"}]
    assert read_jobs(lambda job: "ruff: 3 errors", lint).kind == "other"
    failed = [{"id": 5, "name": "Unit Tests", "conclusion": "failure"}]
    reading = read_jobs(lambda job: "FAILED tests/test_a.py::test_x - assert 1 == 2\n", failed)
    assert reading.kind == "tests" and reading.tests == ("tests/test_a.py::test_x",)


# --- I: the same failure twice ------------------------------------------------------------------


def _two_runs(first: list[str], second: list[str], changed: list[str]):
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5, attempt=2)])
    forge.attempts = {1: first, 2: second}
    forge.files["c1"] = changed

    def attempt_jobs(repo, run_id, attempt):
        return [{"id": 100 + attempt, "name": "Unit Tests", "conclusion": "failure"}]

    forge.attempt_jobs = attempt_jobs
    forge.jobs = lambda repo, run_id: attempt_jobs(repo, run_id, 2)
    forge.logs = {101 + 0: "".join(f"FAILED {t} - x\n" for t in first), 102: "".join(f"FAILED {t} - x\n" for t in second)}
    forge.job_log = lambda repo, job_id: forge.logs.get(int(job_id), "")
    return forge


TOUCH_A = "src/app/tests/test_widget_core.py::test_a"
TOUCH_B = "src/app/tests/test_widget_core.py::test_b"
CHANGED = ["src/app/widget.py"]


def test_c1_2037_i_two_different_failures_are_not_a_repeat_even_when_both_touch_the_merge():
    forge = _two_runs([TOUCH_A], [TOUCH_B], CHANGED)
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.ASK, decision.reasons
    said = " ".join(decision.reasons + decision.attempts)
    assert "not a repeat" in said and TOUCH_A in said and TOUCH_B in said
    assert decision.blamed == []


def test_c1_2037_i_the_same_touching_test_in_both_runs_is_reverted_and_the_revert_says_so():
    forge = _two_runs([TOUCH_A], [TOUCH_A], CHANGED)
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.REVERT, decision.reasons
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    message = git.calls[0]["message"]
    assert "the same test failed in the first run and in the re-run" in message and TOUCH_A in message


def test_c1_2037_i_only_the_repeated_test_is_blamed_when_the_runs_overlap():
    other = "tests/infra/test_toml_cache.py::test_tc6"
    forge = _two_runs([TOUCH_A, other], [TOUCH_A], CHANGED)
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.REVERT
    assert [b["test"] for b in decision.blamed] == [TOUCH_A]


def test_c1_2037_i_a_repeated_test_the_merge_does_not_touch_is_still_not_reverted():
    flaky = "tests/infra/test_toml_cache.py::test_tc6"
    forge = _two_runs([flaky], [flaky], CHANGED)
    assert AR.decide(forge, CONFIG, REPO, "c1").action == AR.ASK
