# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-S4: main is checked on every commit, and a breaking commit is reverted
automatically (ADR-257 D6; PRD the loop section 5.2).

The revert names the commit, the failing check and run, and the author, and opens an
ask for the author. A known flake is re-run once before it counts. The limits: never
revert a revert, at most one commit per failure, nothing while the release lock is
held, and nothing when main's failure predates the commit (an ambiguous range is
asked about, not reverted). It is a dry run unless ``act`` is passed and the
repository variable ``AXIOM_AUTO_REVERT`` is ``1``.

Every forge call hits a fixture, and the git side is a recording stand-in except in
the one test that reverts in a real throwaway repository.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from axiom.extensions.builtins.hygiene._git_isolation import git_isolated_env
from axiom.extensions.builtins.release import auto_revert as AR
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig, parse_ci_health
from axiom.extensions.builtins.release.release_lock import ReleaseLock

REPO = "example-org/app"
REPO_ROOT = Path(__file__).resolve().parents[6]
T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
CONFIG = CIHealthConfig(auto_revert_workflows=("ci.yml",), incident_assignees=("owner",))
TEST_ID = "tests/test_widget.py::test_widget_renders"


def _run(run_id: int, sha: str, conclusion: str, *, minute: int, attempt: int = 1, status: str = "completed") -> dict:
    return {
        "id": run_id,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "head_branch": "main",
        "head_sha": sha,
        "run_attempt": attempt,
        "created_at": (T0 + timedelta(minutes=minute)).isoformat(),
        "head_commit": {"message": f"commit {sha}"},
    }


class Forge:
    """A linear main g0 -> c1 -> c2 with configurable runs. Records every write."""

    def __init__(self, runs: list[dict], *, head: str = "c1") -> None:
        self.runs = runs
        self.head = head
        self.commits: dict[str, dict] = {
            "g0": {"sha": "g0", "message": "green base", "author_login": "someone", "parents": ["root"]},
            "c1": {"sha": "c1", "message": "feat: widget (#41)", "author_login": "dev-one", "parents": ["g0"]},
            "c2": {"sha": "c2", "message": "feat: gadget (#42)", "author_login": "dev-two", "parents": ["c1"]},
        }
        self.order = ["root", "g0", "c1", "c2"]
        self.jobs_by_run: dict[int, list[dict]] = {}
        self.logs: dict[int, str] = {}
        self.files: dict[str, list[str]] = {}
        self.later: list[dict] = []
        self.reruns: list[int] = []
        self.issues: list[dict] = []
        self.comments: list[tuple[int, str]] = []

    def fail_run(self, run_id: int, *, job: str = "Unit Tests", test: str = TEST_ID) -> None:
        self.jobs_by_run[run_id] = [{"id": run_id * 10, "name": job, "conclusion": "failure"}]
        self.logs[run_id * 10] = f"FAILED {test} - AssertionError\n" if test else "lint failed\n"

    # reads
    def workflow_runs(self, repo, workflow, branch, *, per_page=30):
        return [r for r in self.runs if r["path"].endswith(workflow) and r["head_branch"] == branch]

    def branch_head(self, repo, branch):
        return self.head

    def jobs(self, repo, run_id):
        return self.jobs_by_run.get(int(run_id), [])

    def job_log(self, repo, job_id):
        return self.logs.get(int(job_id), "")

    def is_ancestor(self, repo, older, newer):
        return older in self.order and newer in self.order and self.order.index(older) <= self.order.index(newer)

    def commit(self, repo, sha):
        return dict(self.commits[sha])

    def commit_files(self, repo, sha):
        return list(self.files.get(sha, ["src/app/widget.py"]))

    def commits_between(self, repo, base, head):
        return list(self.later)

    def open_issues(self, repo, label):
        return [i for i in self.issues if label in i["labels"]]

    # writes
    def rerun_failed(self, repo, run_id):
        self.reruns.append(int(run_id))

    def create_issue(self, repo, title, body, *, labels, assignees):
        row = {"number": 900 + len(self.issues), "title": title, "body": body, "labels": list(labels),
               "assignees": list(assignees), "html_url": f"https://forge/issues/{900 + len(self.issues)}"}
        self.issues.append(row)
        return row

    def comment_issue(self, repo, number, body):
        self.comments.append((number, body))


class Git:
    def __init__(self, fail: str = "") -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def revert_and_push(self, sha, *, mainline, message, branch):
        self.calls.append({"sha": sha, "mainline": mainline, "message": message, "branch": branch})
        if self.fail:
            raise AR.RevertFailed(self.fail)
        return "r1"


def _red_after_green(*, attempt: int = 1, test: str = TEST_ID) -> Forge:
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5, attempt=attempt)])
    forge.fail_run(2, test=test)
    # A test that failed before on main is not a flake candidate.
    forge.files["c1"] = ["src/app/widget.py"]
    return forge


def _real_red() -> Forge:
    """The failing test's own file changed in the commit, so it is a real red."""
    forge = _red_after_green()
    forge.files["c1"] = ["src/app/widget.py", "tests/test_widget.py"]
    return forge


# -- the revert -------------------------------------------------------------------


@pytest.mark.acceptance("LOOP-S4")
def test_a_commit_that_breaks_main_is_reverted_naming_the_commit_check_run_and_author():
    forge, git, sent = _real_red(), Git(), []
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.REVERT, decision.reasons
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: sent.append((r, a)) or "rcpt")
    assert len(git.calls) == 1
    call = git.calls[0]
    assert call["sha"] == "c1" and call["branch"] == "main" and call["mainline"] is None
    message = call["message"]
    assert message.startswith('Revert "feat: widget (#41)"')
    assert "This reverts commit c1." in message
    assert "Unit Tests" in message
    assert f"https://github.com/{REPO}/actions/runs/2" in message
    assert "@dev-one" in message
    assert "Bypass-Reason:" in message
    assert decision.revert_sha == "r1"
    # The ask for the author: an issue assigned to them, and a notification to them.
    assert len(forge.issues) == 1
    issue = forge.issues[0]
    assert issue["assignees"] == ["dev-one"]
    assert "c1" in issue["title"]
    assert "r1" in issue["body"] and "Unit Tests" in issue["body"]
    assert any(recipient == "@dev-one:github" for recipient, _ in sent)


@pytest.mark.acceptance("LOOP-S4")
def test_a_merge_commit_is_reverted_against_its_first_parent():
    forge, git = _real_red(), Git()
    forge.commits["c1"]["parents"] = ["g0", "side"]
    AR.carry_out(forge, CONFIG, AR.decide(forge, CONFIG, REPO, "c1"), git=git, act=True, sender=lambda r, a: None)
    assert git.calls[0]["mainline"] == 1


@pytest.mark.acceptance("LOOP-S4")
def test_the_default_is_a_dry_run_that_writes_nothing():
    forge, git = _real_red(), Git()
    decision = AR.carry_out(forge, CONFIG, AR.decide(forge, CONFIG, REPO, "c1"), git=git, act=False)
    assert decision.action == AR.REVERT
    assert git.calls == [] and forge.issues == [] and forge.reruns == []
    assert "dry run" in decision.render()


@pytest.mark.acceptance("LOOP-S4")
def test_act_does_nothing_unless_the_repository_variable_is_one(monkeypatch, tmp_path):
    from axiom.extensions.builtins.release.skills import auto_revert as skill
    from axiom.infra.skills import SkillContext

    ctx = SkillContext(registry=None, state_dir=tmp_path, logger=None, user_prompt=None)  # type: ignore[arg-type]
    monkeypatch.setenv("AXIOM_RELEASE_LOCK", "")
    monkeypatch.delenv("AXIOM_AUTO_REVERT", raising=False)
    forge, git = _real_red(), Git()
    params = {"repo": REPO, "sha": "c1", "act": True, "quiet": True, "_client": forge, "_git": git, "_sender": lambda r, a: None, "_config": CONFIG}
    result = skill(params, ctx)
    assert git.calls == [] and forge.issues == []
    assert "AXIOM_AUTO_REVERT" in " ".join(result.value["reasons"])
    monkeypatch.setenv("AXIOM_AUTO_REVERT", "1")
    result = skill(params, ctx)
    assert len(git.calls) == 1
    assert result.value["action"] == AR.REVERT


@pytest.mark.acceptance("LOOP-S4")
def test_a_revert_that_does_not_apply_becomes_an_ask():
    forge, git = _real_red(), Git(fail="conflict in src/app/widget.py")
    decision = AR.carry_out(forge, CONFIG, AR.decide(forge, CONFIG, REPO, "c1"), git=git, act=True, sender=lambda r, a: None)
    assert decision.action == AR.ASK
    assert decision.revert_sha == ""
    assert len(forge.issues) == 1 and "conflict" in forge.issues[0]["body"]


# -- a failure is real before anything is reverted ---------------------------------


@pytest.mark.acceptance("LOOP-S4")
def test_a_cancelled_run_is_not_a_failure():
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "cancelled", minute=5)])
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.NONE
    assert "not a failure" in " ".join(decision.reasons)


@pytest.mark.acceptance("LOOP-S4")
def test_a_run_still_going_is_waited_for():
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "", minute=5, status="in_progress")])
    assert AR.decide(forge, CONFIG, REPO, "c1").action == AR.WAIT


@pytest.mark.acceptance("LOOP-S4")
def test_a_failed_run_that_names_no_failing_check_is_asked_about_not_reverted():
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "failure", minute=5)])
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.ASK
    assert "no failing check" in " ".join(decision.reasons)


@pytest.mark.acceptance("LOOP-S4")
def test_a_known_flake_is_re_run_once_before_it_counts():
    forge, git = _red_after_green(), Git()
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.RERUN, decision.reasons
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert forge.reruns == [2]
    assert git.calls == []


@pytest.mark.acceptance("LOOP-S4")
def test_a_flake_that_fails_again_on_its_re_run_is_reverted():
    forge = _red_after_green(attempt=2)
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.REVERT, decision.reasons
    assert decision.flake.get("verdict") == "real"


@pytest.mark.acceptance("LOOP-S4")
def test_a_failure_with_no_test_ids_is_real_without_a_re_run():
    forge = _red_after_green(test="")
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.REVERT, decision.reasons


# -- the limits ---------------------------------------------------------------------


@pytest.mark.acceptance("LOOP-S4")
def test_a_revert_of_a_revert_needs_a_person():
    forge = _real_red()
    forge.commits["c1"]["message"] = 'Revert "feat: widget (#40)"\n\nThis reverts commit abc.'
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.ASK
    assert "revert of a revert needs a person" in " ".join(decision.reasons)


@pytest.mark.acceptance("LOOP-S4")
def test_nothing_is_reverted_while_the_release_lock_is_held():
    forge, git = _real_red(), Git()
    lock = ReleaseLock(repo=REPO, reason="release 1.2.3", held_by="@rivet", taken_at=T0.isoformat(),
                       expires_at=(T0 + timedelta(minutes=30)).isoformat())
    decision = AR.decide(forge, CONFIG, REPO, "c1", lock=lock)
    assert decision.action == AR.HELD
    AR.carry_out(forge, CONFIG, decision, git=git, act=True, sender=lambda r, a: None)
    assert git.calls == [] and forge.issues == []
    assert "release lock" in decision.render()


@pytest.mark.acceptance("LOOP-S4")
def test_nothing_is_done_when_main_was_already_red_before_the_commit():
    forge = Forge([
        _run(1, "g0", "success", minute=0),
        _run(2, "c1", "failure", minute=5),
        _run(3, "c2", "failure", minute=10),
    ], head="c2")
    forge.fail_run(2)
    forge.fail_run(3)
    forge.files["c2"] = ["tests/test_widget.py"]
    decision = AR.decide(forge, CONFIG, REPO, "c2")
    assert decision.action == AR.NONE
    assert "predates" in " ".join(decision.reasons) and "c1" in " ".join(decision.reasons)


@pytest.mark.acceptance("LOOP-S4")
def test_an_untested_commit_between_green_and_red_is_asked_about_not_reverted():
    forge = Forge([
        _run(1, "g0", "success", minute=0),
        _run(2, "c1", "cancelled", minute=5),
        _run(3, "c2", "failure", minute=10),
    ], head="c2")
    forge.fail_run(3)
    forge.files["c2"] = ["tests/test_widget.py"]
    decision = AR.decide(forge, CONFIG, REPO, "c2")
    assert decision.action == AR.ASK
    assert decision.untested == ["c1"]
    assert "ambiguous" in " ".join(decision.reasons)


@pytest.mark.acceptance("LOOP-S4")
def test_no_green_head_in_reach_is_asked_about_not_reverted():
    forge = Forge([_run(2, "c1", "failure", minute=5)])
    forge.fail_run(2)
    forge.files["c1"] = ["tests/test_widget.py"]
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.ASK
    assert "no green" in " ".join(decision.reasons)


@pytest.mark.acceptance("LOOP-S4")
def test_at_most_one_commit_per_failure():
    forge = _real_red()
    forge.head = "r0"
    forge.later = [{"sha": "r0", "message": 'Revert "feat: widget (#41)"\n\nThis reverts commit c1.', "parents": ["c1"]}]
    decision = AR.decide(forge, CONFIG, REPO, "c1")
    assert decision.action == AR.NONE
    assert "already reverted" in " ".join(decision.reasons)
    # And an ask already open for the commit is commented on, not duplicated.
    forge2, git = _real_red(), Git(fail="conflict")
    for _ in range(2):
        AR.carry_out(forge2, CONFIG, AR.decide(forge2, CONFIG, REPO, "c1"), git=git, act=True, sender=lambda r, a: None)
    assert len(forge2.issues) == 1 and len(forge2.comments) == 1


@pytest.mark.acceptance("LOOP-S4")
def test_every_required_workflow_is_judged():
    forge = Forge([_run(1, "g0", "success", minute=0), _run(2, "c1", "success", minute=5)])
    guard_red = _run(3, "c1", "failure", minute=6)
    guard_red.update(path=".github/workflows/loop-guards.yml", name="Loop guards")
    guard_green = _run(4, "g0", "success", minute=1)
    guard_green.update(path=".github/workflows/loop-guards.yml", name="Loop guards")
    forge.runs += [guard_green, guard_red]
    forge.fail_run(3, job="Guards", test="")
    config = replace(CONFIG, auto_revert_workflows=("ci.yml", "loop-guards.yml"))
    decision = AR.decide(forge, config, REPO, "c1")
    assert decision.action == AR.REVERT, decision.reasons
    assert decision.workflow == "loop-guards.yml" and decision.failing_checks == ["Guards"]


@pytest.mark.acceptance("LOOP-S4")
def test_a_required_workflow_the_repository_does_not_have_yet_is_skipped_not_blinding():
    from axiom.extensions.builtins.release.ci_health.client import CIClientError

    forge = _real_red()
    plain = forge.workflow_runs

    def runs(repo, workflow, branch, *, per_page=30):
        if workflow == "loop-guards.yml":
            raise CIClientError("gh api repos/x/actions/workflows/loop-guards.yml/runs: gh: Not Found (HTTP 404)")
        return plain(repo, workflow, branch, per_page=per_page)

    forge.workflow_runs = runs
    config = replace(CONFIG, auto_revert_workflows=("ci.yml", "loop-guards.yml"))
    decision = AR.decide(forge, config, REPO, "c1")
    assert decision.action == AR.REVERT, decision.reasons + decision.errors
    assert any("loop-guards.yml is not on this repository" in r for r in decision.reasons)
    # A forge that cannot be read for any other reason still concludes nothing.
    def broken(repo, workflow, branch, *, per_page=30):
        raise CIClientError("HTTP 502")

    forge.workflow_runs = broken
    assert AR.decide(forge, config, REPO, "c1").action == AR.NONE


@pytest.mark.acceptance("LOOP-S4")
def test_this_repository_judges_ci_and_the_server_guards():
    config = parse_ci_health({"ci_health": {"auto_revert_workflows": ["ci.yml", "loop-guards.yml"]}})
    assert config.auto_revert_workflows == ("ci.yml", "loop-guards.yml")
    import tomllib

    data = tomllib.loads((REPO_ROOT / ".axiom" / "release-lander.toml").read_text(encoding="utf-8"))
    assert parse_ci_health(data).auto_revert_workflows == ("ci.yml", "loop-guards.yml")


# -- the real git revert --------------------------------------------------------------


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=git_isolated_env(), capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.acceptance("LOOP-S4")
def test_the_revert_is_an_ordinary_commit_pushed_to_main(tmp_path: Path):
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _g(tmp_path, "clone", "-q", str(origin), str(work))
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _g(work, "config", key, value)
    (work / "a.txt").write_text("one\n")
    _g(work, "add", "a.txt")
    _g(work, "commit", "-q", "-m", "seed")
    (work / "a.txt").write_text("two\n")
    _g(work, "commit", "-q", "-am", "break it")
    bad = _g(work, "rev-parse", "HEAD")
    _g(work, "push", "-q", "origin", "HEAD:main")
    git = AR.GitRevert(work, env=git_isolated_env())
    new = git.revert_and_push(bad, mainline=None, message=f'Revert "break it"\n\nThis reverts commit {bad}.\n', branch="main")
    assert _g(origin, "rev-parse", "main") == new
    assert _g(origin, "show", "main:a.txt") == "one"
    assert f"This reverts commit {bad}." in _g(origin, "log", "-1", "--format=%B", "main")


# -- the workflow --------------------------------------------------------------------


@pytest.mark.acceptance("LOOP-S4")
def test_the_workflow_runs_on_main_s_completion_and_acts_only_when_turned_on():
    text = (REPO_ROOT / ".github" / "workflows" / "loop-auto-revert.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    on = workflow.get("on") or workflow.get(True)
    trigger = on["workflow_run"]
    assert set(trigger["workflows"]) == {"CI", "Loop guards"}
    assert trigger["types"] == ["completed"] and trigger["branches"] == ["main"]
    job = workflow["jobs"]["auto-revert"]
    assert "github.event.workflow_run.event == 'push'" in job["if"]
    assert "cancelled" not in job["if"]
    run = "\n".join(str(s.get("run") or "") for s in job["steps"])
    assert "-m axiom.extensions.builtins.release.cli auto-revert" in run
    assert 'if [ "${AXIOM_AUTO_REVERT:-}" = "1" ]' in run
    assert "vars.AXIOM_AUTO_REVERT" in text and "vars.AXIOM_RELEASE_LOCK" in text
    assert "--force" not in text and "--no-verify" not in text
    assert workflow["concurrency"]["cancel-in-progress"] is False
