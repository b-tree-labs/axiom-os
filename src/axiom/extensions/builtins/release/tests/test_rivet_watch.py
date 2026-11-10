# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Blind is not green, main red is loud, and the watch has a watcher.

Every forge answer is a fixture served through a fake client. Nothing here
touches a network, and nothing here sends a message.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.release import ci_monitor
from axiom.extensions.builtins.release.ci_health import main_watch as W
from axiom.extensions.builtins.release.ci_health.client import CIClientError, GhClient
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig, parse_ci_health
from axiom.extensions.builtins.release.ci_health.report import gather, render_text, sweep
from axiom.extensions.builtins.release.ci_health.skill import ci_health, ci_lint
from axiom.extensions.builtins.release.pipeline_status import PipelineStatus
from axiom.extensions.builtins.release.watch_integrity import BLIND, GREEN, RED

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
REPO = "example-org/app"
CONFIG = CIHealthConfig(selection_workflows=("CI",), chronic_red_runs=3, escape_window_days=14)


def stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    run_id: int,
    conclusion: str | None,
    hours_ago: float,
    *,
    sha: str = "",
    message: str = "change",
    workflow: str = "CI",
    event: str = "push",
    branch: str = "main",
) -> dict:
    created = stamp(hours_ago)
    return {
        "id": run_id,
        "name": workflow,
        "workflow_id": 7,
        "path": ".github/workflows/ci.yml",
        "event": event,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "head_branch": branch,
        "head_sha": sha or f"sha{run_id}",
        "run_attempt": 1,
        "created_at": created,
        "run_started_at": created,
        "updated_at": stamp(max(hours_ago - 0.5, 0)),
        "head_commit": {"message": message},
        "html_url": f"https://forge.invalid/run/{run_id}",
        "pull_requests": [],
    }


class FakeForge:
    """Reads come from attributes; every write is recorded."""

    def __init__(self, rows: list[dict] | None = None, *, head: str = "") -> None:
        self.rows = rows or []
        self.head = head
        self.issues: list[dict] = []
        self.writes: list[tuple] = []
        self.runs_error: Exception | None = None
        self.issues_error: Exception | None = None
        self.workflows: dict[str, str] | None = None

    # the reads gather needs
    def billing_usage(self, org: str, year: int, month: int) -> list[dict]:
        return []

    def budgets(self, org: str) -> list[dict]:
        return []

    def runs(self, repo: str, *, created_since: str, max_pages: int = 10) -> list[dict]:
        if self.runs_error:
            raise self.runs_error
        return self.rows

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        return []

    def job_log(self, repo: str, job_id: int) -> str:
        return ""

    def is_ancestor(self, repo: str, older: str, newer: str) -> bool:
        return False

    def branch_protection(self, repo: str, branch: str) -> dict | None:
        return None

    def branch_rules(self, repo: str, branch: str) -> list[dict]:
        return []

    def merged_pulls(self, repo: str, *, since: str, base: str) -> list[dict]:
        return []

    def pull_files(self, repo: str, number: int) -> list[str]:
        return []

    # the reads the outside watch needs
    def workflow_runs(self, repo: str, workflow: str, branch: str, *, per_page: int = 30) -> list[dict]:
        if self.runs_error:
            raise self.runs_error
        return self.rows

    def branch_head(self, repo: str, branch: str) -> str:
        return self.head

    def open_issues(self, repo: str, label: str) -> list[dict]:
        if self.issues_error:
            raise self.issues_error
        return self.issues

    def workflow_files(self, repo: str, ref: str) -> dict[str, str]:
        if self.workflows is None:
            raise CIClientError("404")
        return self.workflows

    # the writes
    def create_issue(self, repo: str, title: str, body: str, *, labels, assignees) -> dict:
        self.writes.append(("create", title, tuple(labels), tuple(assignees)))
        return {"number": 900, "html_url": "https://forge.invalid/issues/900", "body": body}

    def edit_issue(self, repo: str, number: int, *, body: str) -> None:
        self.writes.append(("edit", number, body))

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        self.writes.append(("comment", number, body))

    def close_issue(self, repo: str, number: int) -> None:
        self.writes.append(("close", number))


GREEN_THEN_RED = [
    run(4, "failure", 5, sha="red2", message="later (#12)"),
    run(3, "failure", 7, sha="red1", message="the culprit (#11)"),
    run(2, "cancelled", 8, sha="skip"),
    run(1, "success", 9, sha="good"),
]


# -- the heartbeat -----------------------------------------------------------


def status(state: str) -> PipelineStatus:
    return PipelineStatus(repo="acme/x", provider="github", ref="main", status=state)


def _entries(monkeypatch, entries: list[dict], statuses: list) -> None:
    feed = iter(statuses)

    def _next(_entry):
        item = next(feed)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ci_monitor, "load_watched_repos", lambda: entries)
    monkeypatch.setattr(ci_monitor, "_status_for_entry", _next)


REAL_ENTRY = {"provider": "github", "host": "github.com", "project": "acme/x"}


def test_no_pipelines_is_blind_and_never_green(monkeypatch):
    _entries(monkeypatch, [REAL_ENTRY], [None])
    pipelines, seen = ci_monitor.observe_pipelines()
    assert pipelines == []
    assert seen.state == BLIND and not seen.all_green
    assert "acme/x" in seen.reasons[0]


def test_no_entries_is_blind(monkeypatch):
    _entries(monkeypatch, [], [])
    assert ci_monitor.observe_pipelines()[1].state == BLIND


def test_a_target_that_raises_is_reported_without_its_message(monkeypatch):
    """An exception can carry a URL with a token in it, so only its type is kept."""
    _entries(monkeypatch, [REAL_ENTRY], [RuntimeError("https://token@host")])
    _, seen = ci_monitor.observe_pipelines()
    assert seen.state == BLIND
    assert "token" not in " ".join(seen.reasons)


def test_a_seen_green_pipeline_is_green(monkeypatch):
    _entries(monkeypatch, [REAL_ENTRY], [status("success")])
    assert ci_monitor.observe_pipelines()[1].state == GREEN


def test_a_seen_failed_pipeline_is_red(monkeypatch):
    _entries(monkeypatch, [REAL_ENTRY], [status("failed")])
    assert ci_monitor.observe_pipelines()[1].state == RED


def test_build_status_does_not_call_an_empty_set_green(monkeypatch):
    _entries(monkeypatch, [REAL_ENTRY], [None])
    monkeypatch.setattr(
        "axiom.extensions.builtins.release.mode.detect_mode",
        lambda: SimpleNamespace(to_dict=lambda: {}),
    )
    built = ci_monitor.get_build_status()
    assert built["all_green"] is False
    assert built["state"] == BLIND
    assert built["blind_reasons"]


# -- ci_health ---------------------------------------------------------------


def _gather(forge: FakeForge, tmp_path: Path, config: CIHealthConfig = CONFIG):
    return gather(forge, config, repos=(REPO,), state_dir=tmp_path, now=NOW, read_logs=False)


def test_ci_health_with_no_runs_is_blind(tmp_path: Path):
    report = _gather(FakeForge([]), tmp_path)
    assert report.watch.state == BLIND
    assert report.to_dict()["watch"]["state"] == BLIND
    assert "BLIND" in render_text(report)


def test_ci_health_with_unreadable_runs_is_blind_and_names_the_repo(tmp_path: Path):
    forge = FakeForge()
    forge.runs_error = CIClientError("gh api: 403")
    report = _gather(forge, tmp_path)
    assert report.watch.state == BLIND
    assert REPO in report.watch.reasons[0]


def test_ci_health_on_a_red_main_is_red(tmp_path: Path):
    assert _gather(FakeForge(GREEN_THEN_RED), tmp_path).watch.state == RED


def test_ci_health_on_a_green_main_is_green(tmp_path: Path):
    assert _gather(FakeForge([run(1, "success", 2)]), tmp_path).watch.state == GREEN


def test_the_read_skill_fails_rather_than_reporting_blind_as_fine(tmp_path: Path):
    ctx = SimpleNamespace(state_dir=tmp_path)
    result = ci_health({"repo": REPO, "_client": FakeForge([]), "quiet": True}, ctx)
    assert not result.ok
    assert result.errors
    assert result.value["watch"]["state"] == BLIND


# -- alerts: why the read path said alerts [] --------------------------------


def _kinds(report) -> set[str]:
    return {a.kind for a in report.alerts}


def test_the_read_path_reports_what_a_sweep_would_alert(tmp_path: Path):
    """``alerts: []`` from a read was the bug: only sweep ever computed them."""
    report = _gather(FakeForge(GREEN_THEN_RED), tmp_path)
    assert "red_main" in _kinds(report)
    assert report.sent == []


def test_red_main_names_the_culprit_and_the_incident(tmp_path: Path):
    forge = FakeForge(GREEN_THEN_RED)
    forge.issues = [{"number": 77, "body": W.marker("main"), "html_url": "https://forge.invalid/issues/77"}]
    alert = next(a for a in _gather(forge, tmp_path).alerts if a.kind == "red_main")
    assert alert.severity == "urgent"
    assert "#11" in alert.summary
    assert "https://forge.invalid/issues/77" in alert.body()


def _sweep(forge: FakeForge, tmp_path: Path, now: datetime, sender) -> object:
    return sweep(forge, CONFIG, repos=(REPO,), state_dir=tmp_path, act=False, notify=True, sender=sender, now=now)


def test_red_main_repeats_on_the_interval_and_is_deduped_inside_it(tmp_path: Path):
    calls: list[str] = []

    def sender(_recipient, alert):
        if alert.kind == "red_main":
            calls.append(alert.dedup_key)
        return "receipt"

    forge = FakeForge(GREEN_THEN_RED)
    _sweep(forge, tmp_path, NOW, sender)
    _sweep(forge, tmp_path, NOW + timedelta(minutes=30), sender)
    assert len(calls) == 1, "a second sweep inside the interval must stay quiet"
    _sweep(forge, tmp_path, NOW + timedelta(hours=CONFIG.red_main_repeat_hours), sender)
    assert len(calls) == 2, "still red after the interval must alert again"
    assert all("red1" in key for key in calls), "one spell, one identity"


def test_a_new_spell_alerts_at_once(tmp_path: Path):
    calls: list[str] = []

    def sender(_recipient, alert):
        if alert.kind == "red_main":
            calls.append(alert.dedup_key)
        return "receipt"

    _sweep(FakeForge(GREEN_THEN_RED), tmp_path, NOW, sender)
    later = [run(6, "failure", 1, sha="red9"), run(5, "success", 2, sha="fixed"), *GREEN_THEN_RED]
    _sweep(FakeForge(later), tmp_path, NOW + timedelta(minutes=30), sender)
    assert len(calls) == 2
    assert "red9" in calls[1]


def test_red_main_with_no_culprit_in_the_window_still_alerts(tmp_path: Path):
    """No green before the spell means no culprit, and chronic_red still sees it."""
    rows = [run(3, "failure", 3), run(2, "failure", 4), run(1, "failure", 5)]
    alert = next(a for a in _gather(FakeForge(rows), tmp_path).alerts if a.kind == "red_main")
    assert alert.severity == "urgent"
    assert "culprit" in alert.summary.lower()


def test_chronic_red_on_another_branch_is_not_red_main(tmp_path: Path):
    rows = [run(i, "failure", i, branch="feature") for i in range(1, 4)] + [run(9, "success", 1)]
    assert "red_main" not in _kinds(_gather(FakeForge(rows), tmp_path))


def test_a_red_non_ci_workflow_on_main_is_not_red_main(tmp_path: Path):
    rows = [run(i, "failure", i, workflow="Mirror") for i in range(2, 5)] + [run(1, "success", 1)]
    assert "red_main" not in _kinds(_gather(FakeForge(rows), tmp_path))


def test_failed_or_cancelled_over_threshold_alerts_high(tmp_path: Path):
    rows = [run(i, "failure", i, event="pull_request", branch=f"b{i}") for i in range(1, 5)] + [run(9, "success", 1)]
    alert = next(a for a in _gather(FakeForge(rows), tmp_path).alerts if a.kind == "waste_failed_or_cancelled")
    assert alert.severity == "high"


def test_blind_raises_its_own_alert(tmp_path: Path):
    assert "watch_blind" in _kinds(_gather(FakeForge([]), tmp_path))


def test_an_alert_nobody_received_is_an_error_not_a_silence(tmp_path: Path):
    report = _sweep(FakeForge(GREEN_THEN_RED), tmp_path, NOW, lambda _r, _a: None)
    assert any("not delivered" in e and "red_main" in e for e in report.errors)


def test_a_silent_outside_watch_alerts(tmp_path: Path):
    config = CIHealthConfig(selection_workflows=("CI",), outside_watch_workflow="Lander", outside_watch_stale_hours=3)
    quiet = [run(2, "success", 6, workflow="Lander", event="schedule"), run(1, "success", 1)]
    assert "outside_watch_stale" in _kinds(_gather(FakeForge(quiet), tmp_path, config))
    alive = [run(2, "success", 1, workflow="Lander", event="schedule"), run(1, "success", 1)]
    assert "outside_watch_stale" not in _kinds(_gather(FakeForge(alive), tmp_path, config))


def test_config_reads_the_new_keys():
    cfg = parse_ci_health(
        {
            "ci_health": {
                "red_main_repeat_hours": 2,
                "main_workflow": "build.yml",
                "main_stale_hours": 8,
                "incident_assignees": ["owner"],
                "outside_watch_workflow": "Lander",
                "outside_watch_stale_hours": 4,
            }
        }
    )
    assert (cfg.red_main_repeat_hours, cfg.main_workflow, cfg.main_stale_hours) == (2.0, "build.yml", 8.0)
    assert cfg.incident_assignees == ("owner",)
    assert (cfg.outside_watch_workflow, cfg.outside_watch_stale_hours) == ("Lander", 4.0)


# -- ci_lint -----------------------------------------------------------------


WORKFLOW = "on:\n  pull_request:\njobs:\n  a:\n    runs-on: x\n    timeout-minutes: 30\n    steps: []\n"


def _lint(tmp_path: Path, monkeypatch, params: dict):
    monkeypatch.chdir(tmp_path)
    return ci_lint({"quiet": True, **params}, SimpleNamespace())


def test_lint_with_a_repo_reads_that_repo_not_the_working_directory(tmp_path: Path, monkeypatch):
    forge = FakeForge()
    forge.workflows = {"ci.yml": WORKFLOW}
    result = _lint(tmp_path, monkeypatch, {"repo": REPO, "_client": forge, "protection": False})
    assert result.ok
    assert result.value["workflows_checked"] == 1
    assert result.value["findings"]
    assert result.value["source"] == f"{REPO}@main"


def test_lint_fails_when_the_repo_cannot_be_read(tmp_path: Path, monkeypatch):
    result = _lint(tmp_path, monkeypatch, {"repo": REPO, "_client": FakeForge(), "protection": False})
    assert not result.ok
    assert result.errors
    assert result.value["findings"] == []


def test_lint_fails_when_the_repo_has_no_workflows(tmp_path: Path, monkeypatch):
    forge = FakeForge()
    forge.workflows = {}
    assert not _lint(tmp_path, monkeypatch, {"repo": REPO, "_client": forge}).ok


def test_lint_fails_on_a_directory_with_no_workflows(tmp_path: Path, monkeypatch):
    """The false clean: a workspace root with no workflows returned findings []."""
    assert not _lint(tmp_path, monkeypatch, {}).ok


def test_lint_fails_on_an_explicit_empty_path(tmp_path: Path, monkeypatch):
    assert not _lint(tmp_path, monkeypatch, {"path": str(tmp_path / "missing")}).ok


def test_lint_reads_a_real_directory(tmp_path: Path, monkeypatch):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    result = _lint(tmp_path, monkeypatch, {})
    assert result.ok and result.value["workflows_checked"] == 1


def test_gh_client_fetches_workflow_files():
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        if "contents/.github/workflows?" in args[0]:
            return '[{"name": "ci.yml", "path": ".github/workflows/ci.yml"}, {"name": "notes.md", "path": "x"}]'
        return WORKFLOW

    assert GhClient(runner).workflow_files(REPO, "main") == {"ci.yml": WORKFLOW}
    assert len(calls) == 2


# -- the outside watch -------------------------------------------------------


def read(forge: FakeForge, config: CIHealthConfig = CONFIG, now: datetime = NOW):
    return W.read_main(forge, config, REPO, now=now)


def test_main_reads_green():
    reading = read(FakeForge([run(1, "success", 1, sha="h")], head="h"))
    assert reading.state == GREEN and reading.readable


def test_main_reads_red_with_the_start_of_the_spell_and_its_pull_request():
    reading = read(FakeForge(GREEN_THEN_RED, head="red2"))
    assert reading.state == RED
    assert reading.first_red_sha == "red1"
    assert reading.culprit_pr == 11
    assert reading.category == "unclassified"


def test_a_spell_older_than_the_read_says_at_least():
    rows = [run(2, "failure", 1), run(1, "failure", 2)]
    reading = read(FakeForge(rows, head="sha2"))
    assert reading.state == RED and reading.spell_open_ended


def test_cancelled_runs_are_no_verdict():
    rows = [run(3, "cancelled", 1, sha="h"), run(2, "success", 2, sha="old")]
    assert read(FakeForge(rows, head="h")).state == GREEN


def test_no_verdict_at_all_is_blind():
    assert read(FakeForge([run(1, "cancelled", 1)], head="sha1")).state == BLIND


def test_a_quiet_main_is_not_stale():
    assert read(FakeForge([run(1, "success", 50, sha="h")], head="h")).state == GREEN


def test_a_moved_main_with_no_fresh_verdict_is_blind():
    reading = read(FakeForge([run(1, "success", 50, sha="old")], head="new"))
    assert reading.state == BLIND
    assert "verdict" in reading.reasons[0]


def test_a_read_failure_is_unreadable_and_writes_nothing():
    forge = FakeForge()
    forge.runs_error = CIClientError("503")
    outcome = W.watch_main(forge, CONFIG, REPO, now=NOW, act=True)
    assert outcome.reading.state == BLIND and not outcome.reading.readable
    assert forge.writes == []
    assert not outcome.ok


def test_red_opens_one_incident_with_the_shared_marker():
    forge = FakeForge(GREEN_THEN_RED, head="red2")
    config = CIHealthConfig(incident_assignees=("owner",))
    outcome = W.watch_main(forge, config, REPO, now=NOW, act=True)
    assert outcome.action == "opened" and outcome.ok
    kind, title, labels, assignees = forge.writes[0]
    assert kind == "create" and "main" in title
    assert labels == CONFIG.incident_labels
    assert assignees == ("owner",)


def test_red_updates_the_newest_open_incident_and_keeps_the_rest_of_its_body():
    forge = FakeForge(GREEN_THEN_RED, head="red2")
    forge.issues = [
        {"number": 70, "body": W.marker("main")},
        {"number": 71, "body": f"{W.marker('main')}\noccurrence history"},
        {"number": 72, "body": "something else"},
    ]
    outcome = W.watch_main(forge, CONFIG, REPO, now=NOW, act=True)
    assert outcome.action == "updated" and outcome.number == 71
    assert [w[0] for w in forge.writes] == ["edit"]
    body = forge.writes[0][2]
    assert "occurrence history" in body
    assert "#11" in body
    assert "1 older" in body


def test_a_second_update_replaces_the_section():
    forge = FakeForge(GREEN_THEN_RED, head="red2")
    forge.issues = [{"number": 71, "body": W.marker("main")}]
    W.watch_main(forge, CONFIG, REPO, now=NOW, act=True)
    forge.issues[0]["body"] = forge.writes[-1][2]
    W.watch_main(forge, CONFIG, REPO, now=NOW + timedelta(minutes=30), act=True)
    assert forge.writes[-1][2].count(W.SECTION_BEGIN) == 1


def test_green_closes_only_an_incident_the_watch_adopted():
    forge = FakeForge([run(1, "success", 1, sha="h")], head="h")
    adopted = W.splice(W.marker("main"), "status")
    forge.issues = [{"number": 80, "body": adopted}, {"number": 70, "body": W.marker("main")}]
    outcome = W.watch_main(forge, CONFIG, REPO, now=NOW, act=True)
    assert outcome.action == "closed" and outcome.number == 80
    assert [w[:2] for w in forge.writes] == [("comment", 80), ("close", 80)]


def test_green_with_nothing_adopted_writes_nothing():
    forge = FakeForge([run(1, "success", 1, sha="h")], head="h")
    forge.issues = [{"number": 70, "body": W.marker("main")}]
    outcome = W.watch_main(forge, CONFIG, REPO, now=NOW, act=True)
    assert outcome.action == "none" and forge.writes == []


def test_blind_opens_an_incident_too_and_says_blind():
    forge = FakeForge([run(1, "success", 50, sha="old")], head="new")
    outcome = W.watch_main(forge, CONFIG, REPO, now=NOW, act=True)
    assert outcome.action == "opened"
    assert "blind" in forge.writes[0][1].lower()


def test_without_act_nothing_is_written():
    forge = FakeForge(GREEN_THEN_RED, head="red2")
    outcome = W.watch_main(forge, CONFIG, REPO, now=NOW, act=False)
    assert outcome.action == "would_open"
    assert forge.writes == []


def test_the_incident_says_what_this_watch_cannot_see():
    text = W.render_status(read(FakeForge(GREEN_THEN_RED, head="red2")))
    assert "local heartbeat" in text.lower()


def test_a_failed_write_is_not_ok():
    forge = FakeForge(GREEN_THEN_RED, head="red2")

    def refuse(*_a, **_k):
        raise CIClientError("403")

    forge.create_issue = refuse
    assert not W.watch_main(forge, CONFIG, REPO, now=NOW, act=True).ok


def test_gh_client_lists_issues_without_pull_requests():
    def runner(_args: list[str]) -> str:
        return '[{"number": 2, "body": "a"}, {"number": 1, "body": "b", "pull_request": {}}]'

    assert [i["number"] for i in GhClient(runner).open_issues(REPO, "ci-failure")] == [2]


def test_the_skill_is_cli_only_and_acts_only_when_told(tmp_path: Path):
    from axiom.extensions.builtins.release import skills as S
    from axiom.infra.skills import SkillRegistry

    registry = SkillRegistry()
    S.bind(registry)
    spec = registry.spec("release.ci_main_watch")
    assert spec.surfaces == ("cli",)
    assert spec.side_effects is True

    forge = FakeForge(GREEN_THEN_RED, head="red2")
    ctx = SimpleNamespace(state_dir=tmp_path)
    result = S.ci_main_watch({"repo": REPO, "_client": forge, "quiet": True}, ctx)
    assert result.ok and forge.writes == []
    result = S.ci_main_watch({"repo": REPO, "_client": forge, "quiet": True, "act": True}, ctx)
    assert result.ok and result.value["action"] == "opened"


# -- the workflow ------------------------------------------------------------


def _workflow() -> dict:
    import yaml

    root = Path(__file__).resolve()
    while not (root / ".github").is_dir():
        root = root.parent
    return yaml.safe_load((root / ".github" / "workflows" / "rivet-lander.yml").read_text(encoding="utf-8"))


def test_the_watch_has_its_own_schedule_and_does_not_land():
    doc = _workflow()
    crons = [item["cron"] for item in doc[True]["schedule"]]
    watch_cron = next(c for c in crons if "*/6" not in c)
    jobs = doc["jobs"]
    assert watch_cron in jobs["watch-main"]["if"]
    assert watch_cron in jobs["land"]["if"]
    assert "!=" in jobs["land"]["if"], "a watch tick must not run land --act"
    assert "ci-main-watch" in str(jobs["watch-main"]["steps"])
    assert jobs["watch-main"]["permissions"].get("issues") == "write"
    assert "contents" not in jobs["watch-main"]["permissions"] or jobs["watch-main"]["permissions"]["contents"] == "read"


def test_a_merge_push_does_not_cancel_a_watch():
    doc = _workflow()
    assert "concurrency" not in doc, "workflow-level concurrency would cancel the watch"
    assert doc["jobs"]["land"]["concurrency"]["cancel-in-progress"] is True
    assert doc["jobs"]["watch-main"]["concurrency"]["group"] != doc["jobs"]["land"]["concurrency"]["group"]


# -- the report comment ------------------------------------------------------


def test_the_comment_states_what_landing_will_do():
    import importlib.util

    root = Path(__file__).resolve()
    while not (root / ".github").is_dir():
        root = root.parent
    spec = importlib.util.spec_from_file_location(
        "rivet_comment", root / ".github" / "scripts" / "rivet_warn_comment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = module.comment_body("repo: x")
    assert "(live)" not in body
    assert "changes nothing" in body
    assert "main" in body and "six hours" in body
    assert "act step" in body


def test_the_report_no_longer_says_only_propose_and_report():
    from axiom.extensions.builtins.release.lander import render as R

    text = R.render_text(
        SimpleNamespace(
            repo="x", mode="warn", suite_minutes=1, suite_minutes_source="s", open_count=0, minutes_saved=0,
            batches=[], families=[], superseded=[], ready=[], needs_human=[], waiting_on_ci=[],
            blocked_by_closer=[], notes=[],
        )
    )
    assert "propose and report only" not in text
    assert "changes nothing" in text


@pytest.mark.parametrize("name", ["watch_blind", "outside_watch_stale"])
def test_every_new_alert_kind_has_a_priority_herald_accepts(name: str, tmp_path: Path):
    config = CIHealthConfig(selection_workflows=("CI",), outside_watch_workflow="Lander")
    alert = next(a for a in _gather(FakeForge([]), tmp_path, config).alerts if a.kind == name)
    assert alert.severity in {"low", "normal", "high", "urgent"}
