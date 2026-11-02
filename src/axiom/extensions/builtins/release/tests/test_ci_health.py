# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET CI health: spend, waste detectors, safe cancels, budget-aware landing, lint.

Every forge response is a fixture under ``fixtures/ci_health`` served through
an injected client. Nothing here touches the network.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.ci_health import budget as B
from axiom.extensions.builtins.release.ci_health import detect
from axiom.extensions.builtins.release.ci_health.alerts import Alert, send_alerts
from axiom.extensions.builtins.release.ci_health.cancel import (
    apply_cancels,
    plan_cancels,
    protected_reason,
)
from axiom.extensions.builtins.release.ci_health.client import CIClientError, GhClient
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig, parse_ci_health
from axiom.extensions.builtins.release.ci_health.lint import lint_protection, lint_workflow
from axiom.extensions.builtins.release.ci_health.report import (
    gather,
    render_digest,
    render_text,
    sweep,
)
from axiom.extensions.builtins.release.ci_health.runs import parse_run, parse_runs
from axiom.extensions.builtins.release.ci_health.spend import project, read_spend, summarize
from axiom.extensions.builtins.release.ci_health.update_branch import plan_update_branch
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull

FIX = Path(__file__).parent / "fixtures" / "ci_health"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
REPO = "example-org/app"


def _load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeClient:
    """Serves the fixtures. Records every cancel. Never touches a network."""

    def __init__(self, *, budget_rows=None, billing_error: bool = False, ancestors=None) -> None:
        self.cancelled: list[tuple[str, int]] = []
        self.logs_read: list[int] = []
        self.budget_rows = _load("budgets.json")["budgets"] if budget_rows is None else budget_rows
        self.billing_error = billing_error
        self.ancestors = {("m1", "m2")} if ancestors is None else ancestors

    def billing_usage(self, org: str, year: int, month: int) -> list[dict]:
        assert org == "example-org"
        if self.billing_error:
            raise CIClientError("403 billing scope missing")
        name = f"billing_usage_{year}_{month:02d}.json"
        return _load(name)["usageItems"] if (FIX / name).exists() else []

    def budgets(self, org: str) -> list[dict]:
        return list(self.budget_rows)

    def runs(self, repo: str, *, created_since: str, max_pages: int = 10) -> list[dict]:
        assert created_since in {"2026-10-12", "2026-09-26"}
        return _load("runs.json")["workflow_runs"]

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        return _load("jobs.json").get(str(run_id), {}).get("jobs", [])

    def job_log(self, repo: str, job_id: int) -> str:
        self.logs_read.append(job_id)
        return _load("job_logs.json").get(str(job_id), "")

    def is_ancestor(self, repo: str, older_sha: str, newer_sha: str) -> bool:
        return (older_sha, newer_sha) in self.ancestors

    def branch_protection(self, repo: str, branch: str) -> dict | None:
        return _load("protection.json")

    def branch_rules(self, repo: str, branch: str) -> list[dict]:
        return []

    def cancel_run(self, repo: str, run_id: int) -> None:
        self.cancelled.append((repo, run_id))

    def merged_pulls(self, repo: str, *, since: str, base: str) -> list[dict]:
        return []

    def pull_files(self, repo: str, number: int) -> list[str]:
        return []

    def draft_revert(self, node_id: str, title: str, body: str) -> str:
        raise AssertionError("no revert expected")


def _cfg(**kw) -> CIHealthConfig:
    return replace(CIHealthConfig(), **kw)


def _runs():
    return parse_runs(_load("runs.json")["workflow_runs"], REPO, now=NOW)


def _by_id(runs):
    return {r.id: r for r in runs}


# -- config ------------------------------------------------------------------

def test_thresholds_are_data_from_the_lander_toml():
    cfg = parse_ci_health(
        {"ci_health": {"budget_dollars": 300, "warn_pct": 60, "stop_pct": 85, "run_rate_multiple": 2, "waste_share": 0.2}}
    )
    assert (cfg.budget_dollars, cfg.warn_pct, cfg.stop_pct, cfg.run_rate_multiple, cfg.waste_share) == (
        300.0, 60.0, 85.0, 2.0, 0.2,
    )
    with pytest.raises(LanderRefused):
        parse_ci_health({"ci_health": {"warn_pct": 90, "stop_pct": 70}})
    with pytest.raises(LanderRefused):
        parse_ci_health({"ci_health": {"budget_basis": "list"}})


def test_the_checked_in_lander_toml_carries_the_thresholds():
    from axiom.extensions.builtins.release.ci_health.config import load_ci_health

    root = Path(__file__).resolve().parents[6]
    cfg = load_ci_health(root / ".axiom" / "release-lander.toml")
    assert cfg.warn_pct == 70 and cfg.stop_pct == 90 and cfg.budget_dollars > 0


# -- spend -------------------------------------------------------------------

def test_spend_counts_actions_only_and_splits_by_repo():
    totals = summarize(_load("billing_usage_2026_10.json")["usageItems"], "net")
    assert totals["minutes"] == 27000
    assert totals["net"] == pytest.approx(152.5)
    assert totals["gross"] == pytest.approx(162.5)
    assert totals["by_repo"]["tools"]["minutes"] == 2000


def test_spend_reading_projects_and_compares_with_budget_and_last_month():
    reading = read_spend(FakeClient(), _cfg(), "example-org", now=NOW)
    assert reading.error == ""
    assert reading.spent == pytest.approx(152.5)
    assert reading.budget == 200.0  # the forge budget, since budget_dollars is 0
    assert reading.pct == pytest.approx(76.25)
    elapsed = 25 + 0.5
    assert reading.projected == pytest.approx(152.5 * 31 / elapsed, rel=1e-3)
    assert reading.run_rate_multiple == pytest.approx(reading.projected / 50.0, rel=1e-3)


def test_configured_budget_wins_over_the_forge_budget():
    reading = read_spend(FakeClient(), _cfg(budget_dollars=500), "example-org", now=NOW)
    assert reading.budget == 500


def test_a_failed_billing_read_is_reported_not_guessed():
    reading = read_spend(FakeClient(billing_error=True), _cfg(), "example-org", now=NOW)
    assert reading.error and reading.pct is None


def test_projection_is_linear_to_month_end():
    assert project(100.0, 11, 30, elapsed_fraction=10.0) == pytest.approx(300.0)


def test_gh_client_uses_the_usage_endpoint_not_the_retired_one():
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        return json.dumps({"usageItems": []})

    GhClient(runner).billing_usage("example-org", 2026, 10)
    assert calls[0][0] == "/organizations/example-org/settings/billing/usage?year=2026&month=10"
    assert "settings/billing/actions" not in calls[0][0]


# -- budget state and landing policy -----------------------------------------

@pytest.mark.parametrize(
    "pct,state", [(None, "unknown"), (0, "ok"), (69.9, "ok"), (70, "warn"), (89.9, "warn"), (90, "stop"), (140, "stop")]
)
def test_budget_state_thresholds(pct, state):
    assert B.budget_state(pct, _cfg()) == state


def test_budget_transitions_escalate_and_relax():
    assert B.transition("ok", "warn").escalated
    assert B.transition("warn", "stop").escalated
    assert B.transition(None, "stop").escalated
    assert not B.transition("warn", "warn").escalated
    assert B.transition("stop", "ok").relaxed


def test_landing_policy_per_state():
    ok, warn, stop = (B.landing_policy(s, _cfg()) for s in ("ok", "warn", "stop"))
    assert ok.allow_rerun and ok.batch_min == 1 and not ok.release_only
    assert not warn.allow_rerun and warn.allow_repair and warn.batch_min == 3 and not warn.release_only
    assert not stop.allow_rerun and not stop.allow_repair and not stop.allow_update_branch and stop.release_only


def _pull(number, **kw) -> OpenPull:
    base = dict(number=number, title="t", head_sha=f"sha{number}", head_ref=f"topic/{number}", checks="passing", mergeable=True)
    base.update(kw)
    return OpenPull(**base)


def test_batch_gate_waits_for_a_batch_at_warn():
    warn = B.landing_policy("warn", _cfg())
    fresh = NOW - timedelta(hours=1)
    two = [_pull(1, updated_at=fresh), _pull(2, updated_at=fresh)]
    assert B.batch_gate(two, warn, now=NOW) == (False, "batching: 2 of 3 ready")
    assert B.batch_gate([*two, _pull(3, updated_at=fresh)], warn, now=NOW)[0]
    stale = [_pull(1, updated_at=NOW - timedelta(hours=7))]
    assert B.batch_gate(stale, warn, now=NOW)[0]
    assert B.batch_gate(two, B.landing_policy("ok", _cfg()), now=NOW)[0]


def test_current_state_reads_a_fresh_sweep_and_honours_the_override(tmp_path: Path):
    cfg = _cfg()
    assert B.current_state(tmp_path, cfg, environ={}, now=NOW) == "unknown"
    B.save_state(tmp_path, {"checked_at": NOW.isoformat(), "budget_state": "warn"})
    assert B.current_state(tmp_path, cfg, environ={}, now=NOW) == "warn"
    assert B.current_state(tmp_path, cfg, environ={}, now=NOW + timedelta(hours=3)) == "unknown"
    assert B.current_state(tmp_path, cfg, environ={"AXIOM_CI_BUDGET_STATE": "stop"}, now=NOW) == "stop"


def test_release_pull_by_label_or_branch():
    cfg = _cfg()
    assert B.is_release_pull(_pull(1, labels=("release",)), cfg)
    assert B.is_release_pull(_pull(1, head_ref="release/1.2"), cfg)
    assert not B.is_release_pull(_pull(1), cfg)


# -- detectors -----------------------------------------------------------------

def test_update_branch_reruns_are_detected_by_the_merge_commit():
    runs = _runs()
    signal = detect.update_branch_share(runs, _cfg())
    assert signal.count == 1
    assert signal.minutes == pytest.approx(30.0)
    assert detect.is_update_branch_run(_by_id(runs)[101])
    assert not detect.is_update_branch_run(_by_id(runs)[102])
    assert not detect.is_update_branch_run(replace(_by_id(runs)[101], event="push"))


def test_selection_marker_contract():
    assert detect.parse_selection_marker("x selection: 12/400 reason=affected y") == detect.Selection(12, 400, "affected")
    last = detect.parse_selection_marker("selection: 1/2 reason=affected\nselection: 2/2 reason=map-missing")
    assert last == detect.Selection(2, 2, "map-missing")
    cfg = _cfg()
    assert detect.is_fallback(detect.Selection(400, 400, "base-mismatch"), cfg)
    assert not detect.is_fallback(detect.Selection(400, 400, "full-context"), cfg)
    assert not detect.is_fallback(detect.Selection(12, 400, "affected"), cfg)
    assert not detect.is_fallback(None, cfg)


def test_selection_fallbacks_counts_full_suite_runs_and_unmarked_ones():
    runs = _by_id(_runs())
    samples = [
        (runs[101], "selection: 120/4210 reason=affected"),
        (runs[102], "selection: 4210/4210 reason=base-mismatch"),
        (runs[109], ""),
    ]
    signal = detect.selection_fallbacks(samples, _cfg(), 0)
    assert signal.count == 1
    assert "base-mismatch=1" in signal.detail and "1 without a selection marker" in signal.detail


def test_same_sha_flakes():
    signal = detect.flakes(_runs(), _cfg())
    # run 110 passed on attempt 2; c1 failed in 108 and passed in 109 on one head.
    assert signal.count == 2


def test_chronic_red_branch():
    signal = detect.chronic_red(_runs(), _cfg())
    assert signal.count == 1 and "feat/e" in signal.detail and signal.over


def test_duplicate_main_runs_and_main_push_share():
    runs = _runs()
    dup = detect.duplicate_main_runs(runs, _cfg())
    assert dup.count == 1 and dup.minutes == pytest.approx(30.0)
    main = detect.main_push_share(runs, _cfg())
    assert main.count == 4


def test_failed_or_cancelled_minutes():
    signal = detect.failed_minutes(_runs(), _cfg())
    assert signal.count == 4 and signal.minutes == pytest.approx(50.0)


def test_waste_share_threshold_marks_a_signal_over():
    runs = _runs()
    assert detect.update_branch_share(runs, _cfg(waste_share=0.05)).over
    assert not detect.update_branch_share(runs, _cfg(waste_share=0.9)).over


# -- cancel superseded ---------------------------------------------------------

def test_cancel_superseded_on_a_branch_and_a_descendant_on_main():
    decisions = plan_cancels(_runs(), _cfg(), FakeClient())
    cancel = {d.run.id for d in decisions if d.cancel}
    assert cancel == {103, 106}
    reasons = {d.run.id: d.reason for d in decisions}
    assert reasons[104] == "newest run"
    assert reasons[107] == "newest run"
    assert reasons[116] == "schedule event"


def test_never_cancel_the_newest_or_a_release_or_a_tag():
    runs = _by_id(_runs())
    cfg = _cfg()
    assert protected_reason(runs[105], cfg).startswith("release workflow")
    tag = replace(runs[103], workflow="Build", path=".github/workflows/build.yml", event="push", head_branch="v2.0.0")
    assert protected_reason(tag, cfg) == "tag ref"
    staging = replace(runs[103], workflow="staging checks", path=".github/workflows/staging.yml")
    assert protected_reason(staging, cfg) == ""
    # A release run that is NOT the newest of its group is still never cancelled.
    older = replace(runs[105], id=90, head_sha="r0", created_at=runs[105].created_at - timedelta(minutes=5))
    decisions = plan_cancels([older, runs[105]], cfg, FakeClient())
    assert not any(d.cancel for d in decisions)
    # A tag push not named like a release is still never cancelled.
    t1 = replace(runs[103], id=91, workflow="Build", path="b.yml", event="push", head_branch="v3.0.0", head_sha="t1")
    t2 = replace(t1, id=92, head_sha="t2", created_at=t1.created_at + timedelta(minutes=1))
    assert not any(d.cancel for d in plan_cancels([t1, t2], cfg, FakeClient()))


def test_a_main_run_whose_successor_does_not_descend_is_kept():
    decisions = plan_cancels(_runs(), _cfg(), FakeClient(ancestors=set()))
    assert {d.run.id for d in decisions if d.cancel} == {103}
    assert any(d.reason.startswith("newer head does not descend") for d in decisions)


def test_cancel_cap_per_sweep():
    decisions = plan_cancels(_runs(), _cfg(max_cancels=1), FakeClient())
    assert sum(d.cancel for d in decisions) == 1


def test_apply_cancels_audits_each_and_proposes_without_act(tmp_path: Path):
    client = FakeClient()
    decisions = plan_cancels(_runs(), _cfg(), client)
    proposed = apply_cancels(decisions, client, state_dir=tmp_path, act=False)
    assert client.cancelled == [] and {p["outcome"] for p in proposed} == {"proposed"}
    done = apply_cancels(decisions, client, state_dir=tmp_path, act=True)
    assert sorted(run for _, run in client.cancelled) == [103, 106]
    records = list((tmp_path / "agents" / "rivet" / "ci-health-records").glob("*.json"))
    assert len(records) == 2 and {d["outcome"] for d in done} == {"cancelled"}


# -- update-branch batching ----------------------------------------------------

def test_only_the_next_pull_to_land_is_updated():
    t0 = NOW - timedelta(days=3)
    pulls = [
        _pull(1, created_at=t0, merge_state="BEHIND"),
        _pull(2, created_at=t0 + timedelta(hours=1), merge_state="BEHIND"),
        _pull(3, created_at=t0 + timedelta(hours=2), merge_state="BEHIND"),
        _pull(4, created_at=t0 - timedelta(hours=1), merge_state="BEHIND", checks="failing"),
    ]
    plan = plan_update_branch(pulls)
    assert plan.next == 1 and plan.deferred == (2, 3)


def test_no_update_when_the_next_to_land_is_current_or_updates_are_paused():
    t0 = NOW - timedelta(days=3)
    pulls = [_pull(1, created_at=t0, merge_state="CLEAN"), _pull(2, created_at=t0 + timedelta(hours=1), merge_state="BEHIND")]
    plan = plan_update_branch(pulls)
    assert plan.next is None and plan.deferred == (2,)
    paused = plan_update_branch([_pull(1, created_at=t0, merge_state="BEHIND")], allow=False)
    assert paused.next is None and "paused" in paused.reason


# -- the actor obeys the budget and the update rule ----------------------------

def _actor(tmp_path, pulls, *, budget=None, provider=None, auto=("merge",), effects=None):
    from axiom.extensions.builtins.release.lander.actor import run_actor
    from axiom.extensions.builtins.release.tests.test_review_hold import _Clear, _config
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import InMemoryActionStore

    return run_actor(
        pulls,
        repo="owner/name",
        config=_config(auto_allowed=tuple(auto)),
        state_dir=tmp_path,
        effects=effects or _UpdateEffects(),
        gate=ApprovalGate(InMemoryActionStore()),
        provider=provider or _Clear(),
        environ={},
        budget=budget,
    )


class _UpdateEffects:
    def __init__(self):
        from axiom.extensions.builtins.release.tests.test_webhook_handoff import LabelEffects

        self._inner = LabelEffects()
        self.updated: list[int] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def head(self, number):
        return f"sha{number}"

    def update_branch(self, number, sha):
        self.updated.append(number)
        return f"new{number}"


def test_actor_updates_only_the_next_behind_pull(tmp_path: Path):
    t0 = NOW - timedelta(days=2)
    pulls = [
        _pull(1, created_at=t0, merge_state="BEHIND"),
        _pull(2, created_at=t0 + timedelta(hours=1), merge_state="BEHIND"),
        _pull(3, created_at=t0 + timedelta(hours=2), merge_state="BEHIND"),
    ]
    effects = _UpdateEffects()
    result = _actor(tmp_path, pulls, auto=("merge", "update_branch"), effects=effects)
    assert effects.updated == [1]
    status = {o.number: (o.status, o.reason) for o in result.outcomes}
    assert status[1][0] == "updated"
    assert status[2] == ("waiting", "behind: waits for #1 to land")
    assert status[3] == ("waiting", "behind: waits for #1 to land")


def test_actor_proposes_the_update_when_it_needs_approval(tmp_path: Path):
    effects = _UpdateEffects()
    result = _actor(tmp_path, [_pull(1, merge_state="BEHIND")], effects=effects)
    assert effects.updated == []
    assert result.outcomes[0].reason == "behind: next to land; update_branch needs approval"


def test_actor_at_stop_lands_only_release_pulls(tmp_path: Path):
    stop = B.landing_policy("stop", _cfg())
    effects = _UpdateEffects()
    result = _actor(tmp_path, [_pull(1), _pull(2, labels=("release",))], budget=stop, effects=effects)
    status = {o.number: o.status for o in result.outcomes}
    assert status[1] == "skipped"
    assert status[2] == "landed"


def test_actor_refuses_a_blind_rerun_above_warn(tmp_path: Path):
    from axiom.extensions.builtins.release.fixes import Diagnosis, Fix

    class _Rerun:
        def diagnose(self, pull):
            return Diagnosis("flaky", "timeout", pull.files)

        def draft(self, diagnosis, pull):
            return Fix(summary="rerun", kind="rerun")

    warn = B.landing_policy("warn", _cfg())
    result = _actor(tmp_path, [_pull(1, checks="failing")], budget=warn, provider=_Rerun())
    assert result.outcomes[0].status == "escalated"
    assert result.outcomes[0].reason.startswith("budget-warn: no blind re-run")
    ok = _actor(tmp_path, [_pull(1, checks="failing")], budget=B.landing_policy("ok", _cfg()), provider=_Rerun())
    assert not ok.outcomes[0].reason.startswith("budget")


def test_update_branch_command_is_pinned_and_never_forced():
    from axiom.extensions.builtins.release.lander.actor import update_branch_command

    command = update_branch_command("o/r", 7, "abc")
    assert command[-1] == "expected_head_sha=abc" and "--force" not in command
    with pytest.raises(LanderRefused):
        update_branch_command("o/r", 7, "")


# -- lint -----------------------------------------------------------------------

PR_NO_CONCURRENCY = """
on:
  pull_request:
jobs:
  test:
    timeout-minutes: 40
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
"""

PR_GOOD = """
on:
  pull_request:
    paths-ignore: ["docs/**"]
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  lint:
    timeout-minutes: 5
    runs-on: ubuntu-latest
    steps: [{run: ruff .}]
  test:
    needs: [lint]
    timeout-minutes: 40
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
"""

NIGHTLY = """
on:
  schedule:
    - cron: "0 3 * * *"
jobs:
  compat:
    strategy:
      matrix:
        python: ["3.11", "3.12", "3.13"]
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
"""


def _rules(text: str) -> set[str]:
    return {f.rule for f in lint_workflow("w.yml", text)}


def test_lint_flags_pr_concurrency_fast_stage_and_docs_filter():
    assert _rules(PR_NO_CONCURRENCY) == {"pr-concurrency", "fast-stage", "docs-filter"}


def test_lint_is_quiet_on_a_good_workflow():
    assert _rules(PR_GOOD) == set()


def test_lint_accepts_a_change_detection_job_as_the_docs_filter():
    text = PR_GOOD.replace('    paths-ignore: ["docs/**"]\n', "").replace(
        "  lint:\n", "  changes:\n    runs-on: ubuntu-latest\n    steps: [{uses: dorny/paths-filter@v3}]\n  lint:\n"
    )
    assert "docs-filter" not in _rules(text)


def test_lint_flags_a_daily_matrix_but_not_a_weekly_one():
    assert _rules(NIGHTLY) == {"nightly-matrix"}
    assert _rules(NIGHTLY.replace("0 3 * * *", "0 3 * * 1")) == set()


def test_lint_flags_strict_protection_without_a_merge_queue():
    strict = {"required_status_checks": {"strict": True}}
    assert [f.rule for f in lint_protection(strict, [])] == ["strict-without-queue"]
    assert lint_protection(strict, [{"type": "merge_queue"}]) == []
    assert lint_protection({"required_status_checks": {"strict": False}}, []) == []


def test_land_prints_ci_lint_as_a_warning_and_still_reports(tmp_path: Path, capsys):
    from axiom.extensions.builtins.release.lander.skill import land
    from axiom.infra.skills import SkillContext

    workflows = tmp_path / "wf"
    workflows.mkdir()
    (workflows / "ci.yml").write_text(PR_NO_CONCURRENCY, encoding="utf-8")
    ctx = SkillContext(registry=None, state_dir=tmp_path, logger=None, user_prompt=None)  # type: ignore[arg-type]
    pulls = [{"number": 1, "title": "t", "head_sha": "abc", "head_ref": "topic/1", "checks": "passing"}]
    params = {"pulls": pulls, "repo": "o/r", "workflows": str(workflows), "state_dir": str(tmp_path), "samples": [20]}
    result = land(params, ctx)
    assert result.ok
    assert {f["rule"] for f in result.value["ci_lint"]} == {"pr-concurrency", "fast-stage", "docs-filter"}
    assert "ci lint (warning)" in capsys.readouterr().err


# -- gather, sweep, alerts --------------------------------------------------------

def test_gather_reads_everything_and_changes_nothing(tmp_path: Path):
    client = FakeClient()
    report = gather(client, _cfg(), repos=(REPO,), state_dir=tmp_path, now=NOW)
    assert client.cancelled == []
    assert report.budget_state == "warn"
    assert report.by_event["pull_request"] > 0 and "CI" in report.by_workflow
    assert report.by_job["unit-tests (3.13)"] > 0
    names = {s.name: s for s in report.signals}
    assert names["selection_fallback"].count == 1
    assert names["superseded_running"].count == 2
    assert [f.rule for f in report.lint] == ["strict-without-queue"]
    text = render_text(report)
    assert "warn" in text and "update_branch_reruns" in text
    assert "RIVET weekly CI digest" in render_digest(report)


def test_sweep_cancels_records_state_and_alerts_once(tmp_path: Path):
    client = FakeClient()
    sent: list[tuple[str, Alert]] = []

    def sender(recipient, alert):
        sent.append((recipient, alert))
        return "rcpt-1"

    cfg = _cfg(waste_share=0.2)
    sweep(client, cfg, repos=(REPO,), state_dir=tmp_path, act=True, notify=True, sender=sender, now=NOW)
    assert sorted(run for _, run in client.cancelled) == [103, 106]
    assert B.load_state(tmp_path)["budget_state"] == "warn"
    kinds = {a.kind for _, a in sent}
    assert "budget_warn" in kinds and "projected_overrun" in kinds and "run_rate" in kinds
    for _, alert in sent:
        assert alert.action_taken and alert.decision
        assert "RIVET did:" in alert.body() and "You decide:" in alert.body()
    superseded = [a for _, a in sent if a.kind == "waste_superseded_running"]
    assert superseded and superseded[0].action_taken == "Cancelled 2 superseded runs."
    first = len(sent)
    sweep(FakeClient(), cfg, repos=(REPO,), state_dir=tmp_path, act=False, notify=True, sender=sender, now=NOW)
    assert len(sent) == first  # the same crossing is not sent twice
    assert B.current_state(tmp_path, cfg, environ={}, now=NOW) == "warn"


def test_sweep_alerts_stop_with_the_one_action_and_the_one_decision(tmp_path: Path):
    sent: list[Alert] = []
    cfg = _cfg(budget_dollars=160)
    sweep(FakeClient(), cfg, repos=(REPO,), state_dir=tmp_path, act=True, notify=True,
          sender=lambda r, a: sent.append(a) or "rcpt", now=NOW)
    stop = [a for a in sent if a.kind == "budget_stop"]
    assert stop and stop[0].severity == "urgent"
    assert stop[0].action_taken.startswith("Paused non-release landing")
    assert stop[0].decision.startswith("Raise the budget")


def test_send_alerts_skips_keys_already_sent():
    alert = Alert("k", "high", "s", "did", "decide", "key-1")
    seen = {"key-1"}
    out = send_alerts([alert], ("@operator",), already_sent=seen, sender=lambda r, a: "x")
    assert out[0]["sent"] is False


def test_selection_logs_are_cached_between_sweeps(tmp_path: Path):
    client = FakeClient()
    sweep(client, _cfg(), repos=(REPO,), state_dir=tmp_path, now=NOW)
    first = len(client.logs_read)
    assert first > 0
    again = FakeClient()
    sweep(again, _cfg(), repos=(REPO,), state_dir=tmp_path, now=NOW)
    assert again.logs_read == []


# -- registration -----------------------------------------------------------------

def test_ci_health_is_a_read_skill_on_the_mcp_and_sweep_is_not():
    from axiom.extensions.builtins.release import skills
    from axiom.infra.skills import SkillRegistry

    registry = SkillRegistry()
    skills.bind(registry)
    health = registry.spec("release.ci_health")
    lint = registry.spec("release.ci_lint")
    assert "mcp" in health.surfaces and "mcp" in lint.surfaces
    assert health.side_effects is False
    assert registry.has("release.ci_sweep") and registry.has("release.ci_digest")
    sweep_spec = registry.spec("release.ci_sweep")
    assert sweep_spec is None or "mcp" not in tuple(sweep_spec.surfaces or ())


def test_the_manifest_schedules_an_hourly_sweep_and_a_weekly_digest():
    import tomllib

    from axiom.extensions.builtins.schedule.manifest import parse_manifest_block

    manifest = Path(__file__).resolve().parents[1] / "axiom-extension.toml"
    blocks = tomllib.loads(manifest.read_text(encoding="utf-8"))["extension"]["schedule"]
    parsed = {b["action"]: parse_manifest_block(b) for b in blocks}
    assert parsed["release.ci_sweep"].cadence.interval == timedelta(hours=1)
    assert parsed["release.ci_digest"].cadence.cron == "0 14 * * 1"


def test_cli_exposes_the_ci_verbs():
    from axiom.extensions.builtins.release.cli import build_parser

    parser = build_parser()
    for verb in ("ci-health", "ci-sweep", "ci-lint", "ci-digest"):
        assert parser.parse_args([verb]).verb == verb


def test_parse_run_minutes_for_active_runs_use_now():
    run = parse_run(
        {"id": 1, "status": "in_progress", "run_started_at": "2026-10-26T11:00:00Z", "created_at": "2026-10-26T11:00:00Z"},
        REPO,
        now=NOW,
    )
    assert run.minutes == pytest.approx(60.0)


# -- escape rate and red-main culprit ----------------------------------------
#
# A small forge built in memory: a green main, then PR 7 (narrowed, passed) merges
# and main goes red on a test that read a .yaml the PR changed.

from axiom.extensions.builtins.release.ci_health import escape as E  # noqa: E402

OWNER = "owner-login"


def _forge_run(rid, event, branch, sha, conclusion, created, *, msg="", prs=()):
    return {
        "id": rid, "name": "CI", "workflow_id": 1, "path": ".github/workflows/ci.yml", "event": event,
        "head_branch": branch, "head_sha": sha, "status": "completed", "conclusion": conclusion,
        "run_attempt": 1, "created_at": created, "run_started_at": created, "updated_at": created,
        "head_commit": {"message": msg}, "pull_requests": [{"number": n} for n in prs],
    }


def _forge_pull(number, author, merged, merge_sha, head_sha, title="change"):
    return {
        "number": number, "title": title, "user": {"login": author}, "html_url": f"https://x/pull/{number}",
        "node_id": f"PR_{number}", "merged_at": merged, "merge_commit_sha": merge_sha, "head": {"sha": head_sha},
    }


class Forge(FakeClient):
    def __init__(self, *, pr_runs, main, pulls, files, logs, jobs=None, **kw):
        super().__init__(**kw)
        self.rows = pr_runs + main
        self.pulls = pulls
        self.files = files
        self.logs = logs
        self.job_rows = jobs or {}
        self.reverts: list[tuple[str, str]] = []

    def runs(self, repo, *, created_since, max_pages=10):
        return self.rows

    def jobs(self, repo, run_id):
        return self.job_rows.get(run_id) or [{"id": run_id * 10, "name": "unit (3.13)", "conclusion": "failure"}]

    def job_log(self, repo, job_id):
        return self.logs.get(job_id, "")

    def merged_pulls(self, repo, *, since, base):
        return self.pulls

    def pull_files(self, repo, number):
        return self.files.get(number, [])

    def draft_revert(self, node_id, title, body):
        self.reverts.append((node_id, title))
        return f"https://x/pull/900-{node_id}"


NARROWED = "selection: 12/4000 reason=affected\n"
FULL = "selection: 4000/4000 reason=full-context\n"
RED_LOG = (
    "2026-10-20T10:00:00Z FAILED tests/test_cfg.py::test_loads_yaml - KeyError\n"
    "2026-10-20T10:00:00Z ERROR tests/test_cfg.py::test_other\n"
)


def _forge(*, author=OWNER, pr_reason=NARROWED, main_green_after=False, files=None, pr_log_extra=""):
    pr_runs = [_forge_run(1, "pull_request", "feat/y", "h7", "success", "2026-10-20T08:00:00Z", prs=(7,))]
    main = [
        _forge_run(2, "push", "main", "m0", "success", "2026-10-19T08:00:00Z"),
        _forge_run(3, "push", "main", "m7", "failure", "2026-10-20T09:00:00Z", msg="change (#7)"),
    ]
    if main_green_after:
        main.append(_forge_run(4, "push", "main", "m8", "success", "2026-10-20T12:00:00Z"))
    return Forge(
        pr_runs=pr_runs, main=main,
        pulls=[_forge_pull(7, author, "2026-10-20T08:59:00Z", "m7", "h7", title="tune config")],
        files={7: files or ["config/settings.yaml", "src/app.py"]},
        logs={10: pr_reason + pr_log_extra, 30: RED_LOG},
        jobs={1: [{"id": 10, "name": "unit (3.13)", "conclusion": "success"}]},
    )


def _report(forge, **kw):
    cfg = _cfg(selection_log_sample=5, **kw)
    runs = parse_runs(forge.rows, REPO, now=NOW)
    pulls = E.load_pulls(forge, cfg, REPO, NOW)
    return cfg, runs, pulls


def test_an_escape_is_found_with_its_tests_and_changed_file_types():
    forge = _forge()
    cfg, runs, pulls = _report(forge)
    rep = E.escape_report(forge, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={})
    assert rep.narrowed_merges == 1 and len(rep.escapes) == 1
    item = rep.escapes[0]
    assert item.pull.number == 7
    assert item.tests == ["tests/test_cfg.py::test_loads_yaml", "tests/test_cfg.py::test_other"]
    assert ".yaml" in item.file_types
    assert rep.rate == 1.0 and rep.over
    assert rep.per_30_days == pytest.approx(1.0)  # one escape in a 30 day window


def test_no_escape_when_the_pr_was_not_narrowed_or_main_stays_green():
    full = _forge(pr_reason=FULL)
    cfg, runs, pulls = _report(full)
    rep = E.escape_report(full, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={})
    assert rep.escapes == [] and rep.narrowed_merges == 0 and not rep.over
    ok = _forge()
    ok.rows[-1]["conclusion"] = "success"
    cfg, runs, pulls = _report(ok)
    rep = E.escape_report(ok, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={})
    assert rep.escapes == [] and rep.narrowed_merges == 1 and rep.rate == 0.0


def test_a_failing_test_the_narrowed_run_ran_and_passed_is_not_an_escape():
    forge = _forge(pr_log_extra=(
        "tests/test_cfg.py::test_loads_yaml PASSED\ntests/test_cfg.py::test_other PASSED\n"
    ))
    cfg, runs, pulls = _report(forge)
    rep = E.escape_report(forge, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={})
    assert rep.escapes == []


def test_the_threshold_and_recommendations_are_data_and_only_proposals():
    forge = _forge()
    cfg, runs, pulls = _report(forge, escape_rate_warn=0.5)
    rep = E.escape_report(forge, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={})
    assert any("`*.yaml`" in line and "full-suite triggers" in line and "testmon_blast_radius.py" in line
               for line in rep.recommendations)
    cfg, runs, pulls = _report(forge)
    assert E.escape_report(forge, replace(cfg, escape_rate_warn=1.0), repo=REPO, runs=runs, pulls=pulls,
                           cache={}, failures={}).over
    # python-only escape proposes a lower cap, derived from config
    py = _forge(files=["src/app.py"])
    cfg, runs, pulls = _report(py, cap_fraction=0.4, cap_step=0.5)
    rep = E.escape_report(py, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={})
    assert any("from 0.40 to 0.20" in line for line in rep.recommendations)
    # a rate under the threshold proposes nothing
    forge.rows[-1]["conclusion"] = "success"
    cfg, runs, pulls = _report(forge)
    assert E.escape_report(forge, cfg, repo=REPO, runs=runs, pulls=pulls, cache={}, failures={}).recommendations == []


def test_the_culprit_is_the_first_red_commit_with_its_pr_author_and_tests():
    forge = _forge()
    cfg, runs, pulls = _report(forge)
    found = E.find_culprits(forge, cfg, runs=runs, pulls=pulls, failures={})
    assert len(found) == 1
    c = found[0]
    assert (c.run.head_sha, c.pull.number, c.pull.author, c.open) == ("m7", 7, OWNER, True)
    assert c.tests == ["tests/test_cfg.py::test_loads_yaml", "tests/test_cfg.py::test_other"]
    healed = _forge(main_green_after=True)
    cfg, runs, pulls = _report(healed)
    assert E.find_culprits(healed, cfg, runs=runs, pulls=pulls, failures={})[0].open is False


def _sweep(forge, tmp_path, sent, **kw):
    cfg = _cfg(selection_log_sample=5, revert_authors=(OWNER,), **kw)
    return sweep(forge, cfg, repos=(REPO,), state_dir=tmp_path, act=True, notify=True,
                 now=NOW, sender=lambda r, a: sent.append((r, a)) or "rcpt")


def test_the_red_main_alert_goes_out_once_naming_pr_author_and_tests(tmp_path: Path):
    sent: list = []
    forge = _forge()
    first = _sweep(forge, tmp_path, sent)
    red = [a for _, a in sent if a.kind == "red_main"]
    assert len(red) == 1
    assert "PR #7" in red[0].summary and f"@{OWNER}" in red[0].summary and "test_loads_yaml" in red[0].summary
    assert first.alerts
    again = _sweep(forge, tmp_path, sent)
    assert [a for _, a in sent if a.kind == "red_main"] == red  # no second send
    assert any(s["kind"] == "red_main" and s["why"] == "already sent" for s in again.sent)


def test_a_draft_revert_is_opened_only_for_the_owners_pr_and_only_once(tmp_path: Path):
    forge = _forge()
    rep = _sweep(forge, tmp_path, [])
    assert forge.reverts == [("PR_7", "revert: tune config (main red since 2026-10-20 09:00Z)")]
    assert rep.culprits[0].revert_url.startswith("https://x/pull/900")
    _sweep(forge, tmp_path, [])
    assert len(forge.reverts) == 1  # remembered; not drafted again


def test_no_revert_for_someone_elses_pr_but_the_alert_still_goes(tmp_path: Path):
    forge = _forge(author="someone-else")
    sent: list = []
    rep = _sweep(forge, tmp_path, sent)
    assert forge.reverts == []
    assert "not an owner" in rep.culprits[0].revert_note
    assert any(a.kind == "red_main" and "@someone-else" in a.summary for _, a in sent)


def test_no_revert_on_a_dry_run_or_when_no_owner_is_configured(tmp_path: Path):
    forge = _forge()
    cfg = _cfg(selection_log_sample=5, revert_authors=(OWNER,))
    sweep(forge, cfg, repos=(REPO,), state_dir=tmp_path, act=False, now=NOW)
    assert forge.reverts == []
    sweep(forge, _cfg(selection_log_sample=5), repos=(REPO,), state_dir=tmp_path / "b", act=True, now=NOW)
    assert forge.reverts == []


def test_the_escape_alert_carries_the_proposal_and_edits_nothing(tmp_path: Path):
    forge = _forge()
    sent: list = []
    rep = _sweep(forge, tmp_path, sent)
    alert = next(a for _, a in sent if a.kind == "escape_rate")
    assert "full-suite triggers" in alert.decision
    assert rep.to_dict()["escape"][REPO]["over"] is True
    assert "Escape rate" in render_text(rep) and "Red main since" in render_text(rep)
