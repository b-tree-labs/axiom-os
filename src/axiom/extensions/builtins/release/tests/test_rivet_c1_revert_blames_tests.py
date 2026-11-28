# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1: auto-revert reverts only for failures the merge can explain (#2037).

A merge was reverted for two tests it did not touch, both flaky. Before reverting, the failed
jobs are re-run once (the flake path already does), and then the tests that still fail are
checked against what the merge changed: a revert needs at least one failing test that touches
the changed paths, by the test's own file, a module it imports, the package it sits in, a
matching name, or the repository's test selector. Otherwise nothing is reverted, the one
incident stays open, and it says which tests it blamed and which it did not, and why.

Acceptance ids:

  C1-2037-A  Failing tests the merge does not touch are not reverted; an ask names them and
             why they were not blamed.
  C1-2037-B  A failing test that touches the change still reverts (by file, package, import,
             name, selector), and the revert message names the blamed tests and the evidence.
  C1-2037-C  Mixed: one related failure is enough to revert; the unrelated one is named as
             not blamed.
  C1-2037-D  The incident is one issue: a second pass comments on it, it does not open another.
  C1-2037-E  A failure with no test ids behaves as before, and a first failure is still re-run.
  C1-2037-F  The decision logic is Python with its own unit tests; the workflow only calls it.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.release import auto_revert as AR
from axiom.extensions.builtins.release.blame import blame_tests
from axiom.extensions.builtins.release.selectors import Selection
from axiom.extensions.builtins.release.tests.test_loop_s4_a_commit_that_breaks_main_is_reverted import (
    CONFIG,
    REPO,
    Forge,
    Git,
    _run,
)

FLAKY_A = "tests/infra/test_toml_cache.py::test_tc6_reads_the_cache"
FLAKY_B = "src/axiom/extensions/builtins/program/tests/test_followups_real_postgres.py::test_a_snooze_moves_the_due_time"
CHANGED = ["src/axiom/extensions/builtins/library/access_rows.py", "src/axiom/extensions/builtins/library/skills/rows.py"]


def _after_rerun(*tests: str, changed=None, read_source=None, selector=None):
    """main red on c1, the failed jobs already re-run once (attempt 2), still red."""
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5, attempt=2)])
    forge.jobs_by_run[2] = [{"id": 20, "name": "Unit Tests", "conclusion": "failure"}]
    forge.logs[20] = "".join(f"FAILED {test} - AssertionError\n" for test in tests)
    forge.files["c1"] = list(changed or CHANGED)
    decision = AR.decide(forge, CONFIG, REPO, "c1", read_source=read_source, selector=selector)
    return forge, decision


# --- A: not this merge's tests ----------------------------------------------------------


def test_c1_2037_a_two_flaky_tests_the_merge_does_not_touch_are_not_reverted():
    forge, decision = _after_rerun(FLAKY_A, FLAKY_B)
    assert decision.action == AR.ASK, decision.reasons
    assert decision.blamed == [] and [c["test"] for c in decision.cleared] == [FLAKY_A, FLAKY_B]
    said = " ".join(decision.reasons)
    assert "not reverted" in said and "test_toml_cache" in said and "test_followups_real_postgres" in said
    assert "does not touch" in said


def test_c1_2037_a_the_ask_lists_what_was_not_blamed_and_nothing_is_reverted():
    forge, decision = _after_rerun(FLAKY_A, FLAKY_B)
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert git.calls == []
    assert len(forge.issues) == 1
    body = forge.issues[0]["body"]
    assert "Not blamed" in body and FLAKY_A in body and FLAKY_B in body
    assert "was **not** reverted" in body


def test_c1_2037_d_a_second_pass_comments_on_the_same_incident():
    forge, decision = _after_rerun(FLAKY_A)
    AR.carry_out(forge, CONFIG, decision, git=Git(), act=True, sender=lambda r, a: None)
    again = AR.decide(forge, CONFIG, REPO, "c1")
    AR.carry_out(forge, CONFIG, again, git=Git(), act=True, sender=lambda r, a: None)
    assert len(forge.issues) == 1 and len(forge.comments) == 1


# --- B: a test that touches the change --------------------------------------------------


def test_c1_2037_b_a_test_in_the_package_the_merge_changed_is_blamed_and_the_revert_says_so():
    test = "src/axiom/extensions/builtins/library/tests/test_access_rows.py::test_rows_are_granted"
    forge, decision = _after_rerun(test)
    assert decision.action == AR.REVERT, decision.reasons
    assert [b["test"] for b in decision.blamed] == [test]
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    message = git.calls[0]["message"]
    assert "Blamed tests" in message and test in message
    assert "failed again when only the failed jobs were re-run" in message
    assert "sits in the tests of" in message
    assert "library" in forge.issues[0]["body"] and test in forge.issues[0]["body"]


def test_c1_2037_b_a_test_whose_own_file_changed_is_blamed():
    forge, decision = _after_rerun(FLAKY_A, changed=["tests/infra/test_toml_cache.py"])
    assert decision.action == AR.REVERT
    assert "its own file was changed" in decision.blamed[0]["why"]


def test_c1_2037_b_a_test_that_imports_a_changed_module_is_blamed():
    test = "tests/integration/test_flow.py::test_flow_runs"
    source = {"tests/integration/test_flow.py": "import pytest\nfrom axiom.extensions.builtins.library import access_rows\n"}
    forge, decision = _after_rerun(test, read_source=source.get)
    assert decision.action == AR.REVERT, decision.reasons
    assert "imports" in decision.blamed[0]["why"] and "access_rows" in decision.blamed[0]["why"]


def test_c1_2037_b_a_test_the_selector_names_for_the_changed_paths_is_blamed():
    test = "tests/integration/test_flow.py::test_flow_runs"

    class Selector:
        def select(self, paths, base, head):
            assert list(paths) == CHANGED
            return Selection(False, ("tests/integration/test_flow.py",), "map")

    forge, decision = _after_rerun(test, selector=Selector())
    assert decision.action == AR.REVERT, decision.reasons
    assert "test selector" in decision.blamed[0]["why"]


def test_c1_2037_b_a_selector_that_cannot_narrow_blames_nothing_and_clears_nothing():
    class Whole:
        def select(self, paths, base, head):
            return Selection(True, (), "no map")

    forge, decision = _after_rerun(FLAKY_A, selector=Whole())
    assert decision.action == AR.ASK


def test_c1_2037_b_a_test_named_for_the_changed_module_is_blamed():
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5, attempt=2)])
    forge.fail_run(2, test="tests/test_widget.py::test_widget_renders")
    forge.files["c1"] = ["src/app/widget.py"]
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.REVERT and "name matches" in decision.blamed[0]["why"]


# --- C: mixed ------------------------------------------------------------------------------


def test_c1_2037_c_one_related_failure_reverts_and_the_unrelated_one_is_named_as_not_blamed():
    related = "src/axiom/extensions/builtins/library/tests/test_access_rows.py::test_rows_are_granted"
    forge, decision = _after_rerun(FLAKY_A, related)
    assert decision.action == AR.REVERT
    assert [b["test"] for b in decision.blamed] == [related]
    assert [c["test"] for c in decision.cleared] == [FLAKY_A]
    git = Git()
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    message = git.calls[0]["message"]
    assert "Not blamed" in message and FLAKY_A in message


# --- E: unchanged behaviour -----------------------------------------------------------------


def test_c1_2037_e_a_first_failure_is_still_re_run_before_anything():
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5, attempt=1)])
    forge.fail_run(2, test=FLAKY_A)
    forge.files["c1"] = CHANGED
    assert AR.decide(forge, CONFIG, REPO, "c1").action == AR.RERUN


def test_c1_2037_e_a_failure_with_no_test_ids_is_re_run_never_reverted():
    """Replaces "judged as before" (a revert): that pinned the behaviour that reverted the reland's
    parent for an apt and docker failure. No named failing test means no revert."""
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5)])
    forge.fail_run(2, test="")
    assert AR.decide(forge, CONFIG, REPO, "c1").action == AR.RERUN


# --- F: the pure decision --------------------------------------------------------------------


def test_c1_2037_f_blame_is_a_pure_function_of_tests_and_changed_paths():
    result = blame_tests([FLAKY_A], ["src/app/other.py"])
    assert result.blamed == () and result.cleared[0].test == FLAKY_A
    assert "does not touch" in result.cleared[0].why


def test_c1_2037_f_the_workflow_only_calls_the_decision():
    text = (Path(__file__).resolve().parents[6] / ".github" / "workflows" / "loop-auto-revert.yml").read_text(encoding="utf-8")
    assert "auto-revert" in text and "blame" not in text.lower().replace("blamed", "")
