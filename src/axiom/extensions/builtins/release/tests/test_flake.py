# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Flake handling on main (#1782).

A red main run is a flake when the failing test's first failure on main in N
runs, its file untouched by the merge, and a re-run of only the failed jobs that
passes all hold. RIVET re-runs once itself, files one issue per test, and
``ci_health`` reports green-after-rerun with the flake named. Everything else is
a real red. Every forge call here hits a fixture; no run is ever re-run for real.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.release.ci_health import budget as B
from axiom.extensions.builtins.release.ci_health import flake
from axiom.extensions.builtins.release.ci_health.client import CIClientError, GhClient
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.report import gather, render_text, sweep
from axiom.extensions.builtins.release.ci_health.runs import parse_runs

FIX = Path(__file__).parent / "fixtures" / "ci_health"
NOW = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
REPO = "example-org/app"
TEST = "tests/test_fwd.py::test_forwarder"


def _fixture() -> dict:
    return json.loads((FIX / "flake_main.json").read_text(encoding="utf-8"))


class FlakeClient:
    """Serves ``flake_main.json``. Records every re-run and issue write. No network."""

    def __init__(self, data: dict | None = None) -> None:
        self.data = data or _fixture()
        self.reruns: list[int] = []
        self.issues: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.commit_error = False

    # reads the report needs
    def billing_usage(self, org, year, month):
        return []

    def budgets(self, org):
        return []

    def runs(self, repo, *, created_since, max_pages=10):
        return self.data["workflow_runs"]

    def workflow_runs(self, repo, workflow, branch, *, per_page=30):
        return [r for r in self.data["workflow_runs"] if r["head_branch"] == branch]

    def branch_head(self, repo, branch):
        return [r for r in self.data["workflow_runs"] if r["head_branch"] == branch][-1]["head_sha"]

    def jobs(self, repo, run_id):
        return self.data["jobs"].get(str(run_id), [])

    def job_log(self, repo, job_id):
        return self.data["job_logs"].get(str(job_id), "")

    def is_ancestor(self, repo, older, newer):
        return True

    def branch_protection(self, repo, branch):
        return None

    def branch_rules(self, repo, branch):
        return []

    def merged_pulls(self, repo, *, since, base):
        return []

    def pull_files(self, repo, number):
        return []

    def open_issues(self, repo, label):
        return [i for i in self.issues if label in i["labels"] and i["state"] == "open"]

    def workflow_files(self, repo, ref):
        return {}

    def commit_files(self, repo, sha):
        if self.commit_error:
            raise CIClientError("HTTP 502")
        return list(self.data["commit_files"].get(sha, []))

    # writes
    def cancel_run(self, repo, run_id):
        raise AssertionError("nothing to cancel")

    def rerun_failed(self, repo, run_id):
        self.reruns.append(int(run_id))

    def create_issue(self, repo, title, body, *, labels, assignees):
        number = 500 + len(self.issues)
        row = {"number": number, "title": title, "body": body, "labels": list(labels), "state": "open",
               "html_url": f"https://x/issues/{number}"}
        self.issues.append(row)
        return row

    def edit_issue(self, repo, number, *, body):
        pass

    def comment_issue(self, repo, number, body):
        self.comments.append((number, body))

    def close_issue(self, repo, number):
        pass

    def draft_revert(self, node_id, title, body):
        raise AssertionError("no revert expected")


def _cfg(**kw) -> CIHealthConfig:
    return replace(CIHealthConfig(), **kw)


def _runs(client: FlakeClient):
    return parse_runs(client.data["workflow_runs"], REPO, now=NOW)


def _run(client: FlakeClient, run_id: int):
    return next(r for r in _runs(client) if r.id == run_id)


def _assess(client: FlakeClient, records: dict | None = None, **cfg):
    return flake.assess(client, _cfg(**cfg), repo=REPO, runs=_runs(client), failures={}, records=records if records is not None else {})


# -- the classifier -------------------------------------------------------------------


def test_a_first_failure_with_its_file_untouched_is_a_candidate_awaiting_the_rerun():
    readings = _assess(FlakeClient())
    assert [r.run_id for r in readings] == [304]
    reading = readings[0]
    assert reading.verdict == flake.CANDIDATE
    assert reading.tests == [TEST]
    assert reading.first_failure is True and reading.file_unchanged is True
    assert "re-run" in reading.line()


def test_a_test_that_failed_before_on_main_is_a_real_red():
    client = FlakeClient()
    client.data["job_logs"]["3041"] = "FAILED tests/test_old.py::test_x - AssertionError\n"
    reading = _assess(client)[0]
    assert reading.verdict == flake.REAL
    assert reading.first_failure is False
    assert "failed before" in reading.reasons[0] and "test_x" in reading.reasons[0]


def test_the_history_window_bounds_what_counts_as_before():
    client = FlakeClient()
    client.data["job_logs"]["3041"] = "FAILED tests/test_old.py::test_x - AssertionError\n"
    # Only the one run before 304 is looked at; 302 is out of the window.
    assert _assess(client, flake_history_runs=1)[0].first_failure is True


def test_a_merge_that_changed_the_failing_tests_file_is_a_real_red():
    client = FlakeClient()
    client.data["commit_files"]["m4"].append("tests/test_fwd.py")
    reading = _assess(client)[0]
    assert reading.verdict == flake.REAL
    assert reading.file_unchanged is False
    assert "tests/test_fwd.py" in reading.reasons[0]


def test_no_test_ids_in_the_log_means_unclassified_and_no_rerun():
    client = FlakeClient()
    client.data["job_logs"]["3041"] = "something exploded\n"
    reading = _assess(client)[0]
    assert reading.verdict == flake.UNCLASSIFIED and reading.tests == []


def test_an_unreadable_merge_is_unclassified_not_a_flake():
    client = FlakeClient()
    client.commit_error = True
    reading = _assess(client)[0]
    assert reading.verdict == flake.UNCLASSIFIED and reading.file_unchanged is None


def test_a_green_main_has_no_candidate():
    client = FlakeClient()
    client.data["workflow_runs"][-1]["conclusion"] = "success"
    assert _assess(client) == []


# -- the re-run: once, by RIVET, only with act ----------------------------------------------------


def test_the_sweep_reruns_the_failed_jobs_once_records_it_and_alerts(tmp_path: Path):
    client = FlakeClient()
    sent: list = []
    report = sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=True, notify=True, now=NOW,
                   sender=lambda r, a: sent.append(a) or "rcpt")
    assert client.reruns == [304], "only the failed jobs of the one red run, once"
    reading = report.flakes[0]
    assert reading.verdict == flake.RERUN_REQUESTED
    assert [a.kind for a in sent if a.kind.startswith("flake")] == ["flake_rerun"]
    assert TEST in next(a for a in sent if a.kind == "flake_rerun").summary
    state = B.load_state(tmp_path)
    assert state["flakes"]["304"]["tests"] == [TEST] and state["flakes"]["304"]["attempt_before"] == 1
    # The next sweep sees the run unchanged and asks for nothing more.
    again = sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=True, notify=True, now=NOW,
                  sender=lambda r, a: sent.append(a) or "rcpt")
    assert client.reruns == [304]
    assert again.flakes[0].verdict == flake.RERUN_REQUESTED
    assert [a.kind for a in sent if a.kind == "flake_rerun"] == ["flake_rerun"], "alerted once"


def test_gather_and_a_dry_run_never_rerun(tmp_path: Path):
    client = FlakeClient()
    gather(client, _cfg(), repos=(REPO,), state_dir=tmp_path, now=NOW)
    sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=False, now=NOW)
    assert client.reruns == []


def test_no_rerun_when_the_operator_turned_it_off_or_the_budget_is_at_stop(tmp_path: Path):
    client = FlakeClient()
    report = sweep(client, _cfg(flake_rerun=False), repos=(REPO,), state_dir=tmp_path, act=True, now=NOW)
    assert client.reruns == [] and report.flakes[0].verdict == flake.CANDIDATE
    B.save_state(tmp_path / "b", {"budget_state": B.STOP})
    actions = flake.act(client, _cfg(), report.flakes, repo=REPO, records={}, act=True, allow_rerun=False, now=NOW)
    assert client.reruns == [] and actions == [] and report.flakes[0].verdict == flake.CANDIDATE
    assert "budget" in report.flakes[0].reasons[-1]


# -- after the re-run -----------------------------------------------------------------------


def _record(**over) -> dict:
    return {"304": {"sha": "m4", "tests": [TEST], "attempt_before": 1, "requested_at": NOW.isoformat(), "issues": {}, **over}}


def _after_rerun(conclusion: str, status: str = "completed") -> FlakeClient:
    client = FlakeClient()
    run = client.data["workflow_runs"][-1]
    run["status"], run["conclusion"], run["run_attempt"] = status, conclusion if status == "completed" else None, 2
    return client


def test_a_rerun_that_passed_is_a_flake_and_files_one_issue_per_test(tmp_path: Path):
    client = _after_rerun("success")
    B.save_state(tmp_path, {"flakes": _record()})
    sent: list = []
    report = sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=True, notify=True, now=NOW,
                   sender=lambda r, a: sent.append(a) or "rcpt")
    reading = report.flakes[0]
    assert reading.verdict == flake.FLAKE
    assert client.reruns == [], "the one re-run was already spent"
    assert [i["title"] for i in client.issues] == [f"flake: {TEST}"]
    assert "flake" in client.issues[0]["labels"] and "m4" in client.issues[0]["body"]
    assert reading.issues == {TEST: "https://x/issues/500"}
    # The release decision reads main as green: the head's own run passed, after a re-run.
    head = report.heads[REPO]
    assert head.verdict == "success" and head.after_rerun
    text = render_text(report)
    assert "green after a re-run" in text and f"flake: {TEST}" in text and "issues/500" in text
    assert not report.culprits or not report.culprits[-1].open
    assert any(a.kind == "flake" and TEST in a.summary for a in sent)
    # A second flake of the same test updates the one issue instead of opening another.
    B.save_state(tmp_path, {"flakes": _record()})
    sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=True, now=NOW)
    assert len(client.issues) == 1 and client.comments and client.comments[0][0] == 500


def test_a_rerun_that_failed_again_is_a_real_red_and_the_red_main_rule_applies(tmp_path: Path):
    client = _after_rerun("failure")
    B.save_state(tmp_path, {"flakes": _record()})
    report = sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=True, now=NOW)
    reading = report.flakes[0]
    assert reading.verdict == flake.REAL and "failed again" in reading.reasons[-1]
    assert client.reruns == [] and client.issues == []
    assert report.heads[REPO].verdict == "failure"
    assert report.culprits and report.culprits[-1].open


def test_a_rerun_still_running_is_pending_for_the_head_and_the_classifier(tmp_path: Path):
    client = _after_rerun("", status="in_progress")
    B.save_state(tmp_path, {"flakes": _record()})
    report = sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, act=True, now=NOW)
    assert report.flakes[0].verdict == flake.RERUN_REQUESTED
    assert report.heads[REPO].verdict == "pending"
    assert client.reruns == []


def test_old_records_are_kept_bounded_and_round_trip(tmp_path: Path):
    records = {str(n): {"sha": f"s{n}", "tests": [], "attempt_before": 1, "requested_at": "", "issues": {}} for n in range(80)}
    B.save_state(tmp_path, {"flakes": records})
    sweep(FlakeClient(), _cfg(), repos=(REPO,), state_dir=tmp_path, act=False, now=NOW)
    kept = B.load_state(tmp_path)["flakes"]
    assert len(kept) <= flake.MAX_RECORDS and "79" in kept


# -- the client never re-runs for real in tests ----------------------------------------------


def test_the_gh_client_builds_the_rerun_failed_jobs_call():
    calls: list[list[str]] = []
    client = GhClient(runner=lambda args: calls.append(list(args)) or "{}")
    client.rerun_failed(REPO, 304)
    assert calls == [["-X", "POST", "repos/example-org/app/actions/runs/304/rerun-failed-jobs"]]
    client.commit_files(REPO, "m4")
    assert calls[-1] == ["repos/example-org/app/commits/m4"]


def test_the_fixture_is_not_mutated_between_tests():
    assert copy.deepcopy(_fixture())["workflow_runs"][-1]["conclusion"] == "failure"
