# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One CI-health reading: spend, where minutes go, waste, and what to do.

``gather`` only reads. ``sweep`` is what the schedule runs: gather, cancel
superseded runs (the one automatic fix), record the budget state that
``land --act`` obeys, and alert on a crossing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.ci_health import budget as _budget
from axiom.extensions.builtins.release.ci_health import detect, escape
from axiom.extensions.builtins.release.ci_health.alerts import Alert, Sender, send_alerts
from axiom.extensions.builtins.release.ci_health.cancel import (
    CancelDecision,
    apply_cancels,
    plan_cancels,
)
from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.lint import LintFinding, lint_protection
from axiom.extensions.builtins.release.ci_health.runs import Run, parse_runs
from axiom.extensions.builtins.release.ci_health.spend import SpendReading, read_spend

#: What RIVET does on its own about each waste signal, and what is left to a person.
PLAYBOOK: dict[str, tuple[str, str]] = {
    "update_branch_reruns": (
        "RIVET updates only the pull request it would land next, never every open one.",
        "Enable a merge queue, or turn off the up-to-date requirement on the default branch.",
    ),
    "superseded_running": (
        "RIVET cancels runs superseded by a newer head on the same branch.",
        "Add `concurrency` with `cancel-in-progress` to the workflows named in the report.",
    ),
    "selection_fallback": (
        "Nothing automatic; the fallback is flagged with its reason.",
        "Fix the selection map so pull requests stop falling back to the full suite.",
    ),
    "same_sha_flakes": (
        "At warn and above RIVET refuses blind re-runs.",
        "Fix or quarantine the flaky tests behind these re-runs.",
    ),
    "chronic_red": (
        "RIVET does not re-run these branches.",
        "Fix or close the branches named in the report.",
    ),
    "duplicate_main_runs": (
        "RIVET cancels an older default-branch run when a newer one tests a descendant.",
        "Remove the duplicate trigger so one head runs once.",
    ),
    "main_push_suite": (
        "Nothing automatic.",
        "Skip the full suite on a default-branch push when the merge queue already tested the tree.",
    ),
    "escape_rate": (
        "Nothing automatic; the report proposes a lower cap or a new full-suite trigger.",
        "Apply the proposal in scripts/testmon_blast_radius.py, or accept the rate.",
    ),
    "failed_or_cancelled": (
        "At warn and above RIVET refuses blind retries.",
        "Fix the workflow with the most failed minutes.",
    ),
}


@dataclass
class CIHealthReport:
    checked_at: str
    org: str
    repos: tuple[str, ...]
    window_days: int
    spend: SpendReading | None
    budget_state: str
    previous_state: str
    run_minutes: float
    runs_read: int = 0
    by_repo: dict[str, float] = field(default_factory=dict)
    by_workflow: dict[str, float] = field(default_factory=dict)
    by_event: dict[str, float] = field(default_factory=dict)
    by_job: dict[str, float] = field(default_factory=dict)
    signals: list[detect.Signal] = field(default_factory=list)
    cancels: list[CancelDecision] = field(default_factory=list)
    cancel_results: list[dict] = field(default_factory=list)
    lint: list[LintFinding] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    escape: dict[str, escape.EscapeReport] = field(default_factory=dict)
    culprits: list[escape.Culprit] = field(default_factory=list)
    #: Main-failure test ids already read, by run id. Persisted, not reported.
    failure_cache: dict[str, list[str]] = field(default_factory=dict, repr=False)
    #: Selection markers already read, by run id. Persisted, not reported.
    selection_cache: dict[str, str] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "org": self.org,
            "repos": list(self.repos),
            "window_days": self.window_days,
            "spend": self.spend.to_dict() if self.spend else None,
            "budget_state": self.budget_state,
            "previous_state": self.previous_state,
            "run_minutes": round(self.run_minutes, 1),
            "runs_read": self.runs_read,
            "by_repo": self.by_repo,
            "by_workflow": self.by_workflow,
            "by_event": self.by_event,
            "by_job": self.by_job,
            "signals": [s.to_dict() for s in self.signals],
            "cancels": [d.to_dict() for d in self.cancels if d.cancel],
            "cancel_results": self.cancel_results,
            "lint": [f.to_dict() for f in self.lint],
            "alerts": [a.to_dict() for a in self.alerts],
            "sent": self.sent,
            "errors": self.errors,
            "escape": {repo: e.to_dict() for repo, e in self.escape.items()},
            "culprits": [c.to_dict() for c in self.culprits],
        }


def default_org(config: CIHealthConfig, repos: tuple[str, ...]) -> str:
    if config.org:
        return config.org
    return repos[0].split("/", 1)[0] if repos and "/" in repos[0] else ""


_NO_SELECTION_JOB = "-"


def _selection_samples(
    client: CIClient, runs: list[Run], config: CIHealthConfig, cache: dict
) -> tuple[list[tuple[Run, str]], list[dict]]:
    """Read the selection marker for the newest finished pull-request runs.

    Markers already read are kept in ``cache`` (run id -> marker text), so an
    hourly sweep reads only new logs.
    """
    wanted = [r for r in runs if r.event in detect.PR_EVENTS and r.status == "completed"]
    workflows = set(config.selection_workflows)
    if not workflows and wanted:
        by_workflow = detect.minutes_by(wanted, "workflow")
        workflows = {next(iter(by_workflow))}
    wanted = [r for r in wanted if r.workflow in workflows]
    wanted.sort(key=lambda r: r.created_at, reverse=True)
    samples: list[tuple[Run, str]] = []
    all_jobs: list[dict] = []
    fetched = 0
    patterns = tuple(p.lower() for p in config.selection_jobs)
    for run in wanted:
        if len(samples) >= config.selection_log_sample:
            break
        key = str(run.id)
        if key in cache:
            if cache[key] != _NO_SELECTION_JOB:
                samples.append((run, cache[key]))
            continue
        if fetched >= config.selection_log_sample:
            break
        fetched += 1
        try:
            jobs = client.jobs(run.repo, run.id)
        except CIClientError:
            continue
        all_jobs.extend(jobs)
        text = _NO_SELECTION_JOB
        for job in jobs:
            if any(p in str(job.get("name") or "").lower() for p in patterns):
                try:
                    log = client.job_log(run.repo, int(job.get("id") or 0))
                except CIClientError:
                    log = ""
                found = detect.parse_selection_marker(log or "")
                text = f"selection: {found.selected}/{found.total} reason={found.reason}" if found else ""
                break
        cache[key] = text
        if text != _NO_SELECTION_JOB:
            samples.append((run, text))
    return samples, all_jobs


def _alerts(report: CIHealthReport, config: CIHealthConfig, transition: _budget.Transition, acted: bool) -> list[Alert]:
    out: list[Alert] = []
    spend = report.spend
    month = f"{spend.year}-{spend.month:02d}" if spend else report.checked_at[:7]
    day = report.checked_at[:10]
    cancelled = sum(1 for r in report.cancel_results if r.get("outcome") == "cancelled")
    if spend and spend.pct is not None and transition.escalated and report.budget_state in {_budget.WARN, _budget.STOP}:
        line = f"Actions spend is ${spend.spent:.2f} of ${spend.budget:.2f} ({spend.pct:.0f}%), projected ${spend.projected:.2f} this month."
        if report.budget_state == _budget.STOP:
            did = "Paused non-release landing, branch updates and re-runs"
            did += f"; cancelled {cancelled} superseded runs." if cancelled else "."
            out.append(
                Alert(
                    "budget_stop",
                    "urgent",
                    f"CI budget at the stop line. {line}",
                    did,
                    f"Raise the budget, or hold until the month resets in {spend.days_in_month - spend.day + 1} days.",
                    f"budget:stop:{month}",
                )
            )
        else:
            out.append(
                Alert(
                    "budget_warn",
                    "high",
                    f"CI budget past the warn line. {line}",
                    f"Landing in batches of {config.warn_batch_min}; blind retries refused.",
                    "Approve the batch as it forms, or act on the largest waste signal below.",
                    f"budget:warn:{month}",
                )
            )
    if (
        spend
        and spend.projected_pct is not None
        and spend.projected_pct >= config.stop_pct
        and report.budget_state in {_budget.OK, _budget.WARN}
    ):
        out.append(
            Alert(
                "projected_overrun",
                "high",
                f"At this run-rate Actions spend reaches ${spend.projected:.2f} ({spend.projected_pct:.0f}% of ${spend.budget:.2f}) by month end.",
                "Superseded runs are cancelled each hour; landing batches from the warn line.",
                "Cut the largest waste signal now, or raise the budget before it refuses jobs.",
                f"projected_overrun:{month}",
            )
        )
    multiple = spend.run_rate_multiple if spend else None
    if multiple is not None and multiple >= config.run_rate_multiple:
        out.append(
            Alert(
                "run_rate",
                "high",
                f"Actions run-rate is {multiple:.1f}x last month: projected ${spend.projected:.2f} vs ${spend.last_month:.2f}.",
                "Nothing changed yet; this is the early warning.",
                "Accept the higher month, or act on the top waste signal.",
                f"run_rate:{month}:{int(multiple * 2) / 2:g}",
            )
        )
    for culprit in report.culprits:
        if not culprit.open:
            continue
        pull = culprit.pull
        who = (
            f"PR #{pull.number} \"{pull.title}\" by @{pull.author}"
            if pull
            else (f"PR #{culprit.pr_number}" if culprit.pr_number else "a commit with no pull request")
        )
        if not pull and culprit.candidates:
            who = "one of " + ", ".join(f"#{c.number} by @{c.author}" for c in culprit.candidates)
        tests = ", ".join(culprit.tests[:8]) + (f" (+{len(culprit.tests) - 8} more)" if len(culprit.tests) > 8 else "")
        did = (
            f"Opened a draft revert: {culprit.revert_url}. It is not merged."
            if culprit.revert_url
            else "Named the culprit; no revert opened."
        )
        out.append(
            Alert(
                "red_main",
                "urgent",
                f"Main went red at {culprit.since} on {culprit.run.head_sha[:10]}: {who}. Failing: {tests or 'ids not in the log'}.",
                did,
                "Fix forward, or merge the draft revert if you agree.",
                f"red_main:{culprit.run.head_sha}",
                detail=culprit.run.url,
            )
        )
    for repo, found in report.escape.items():
        if found.over:
            week = datetime.fromisoformat(report.checked_at).isocalendar()
            out.append(
                Alert(
                    "escape_rate",
                    "normal",
                    f"{repo}: {len(found.escapes)} narrowed pull request(s) escaped to a red main of {found.narrowed_merges} "
                    f"({found.rate:.0%}, warn at {found.rate_warn:.0%}).",
                    "Nothing changed; these are proposals.",
                    " ".join(found.recommendations) or "Review the escaped tests.",
                    f"escape_rate:{repo}:{week.year}-W{week.week:02d}",
                )
            )
    for signal in report.signals:
        if not signal.over:
            continue
        did, decide = PLAYBOOK.get(signal.name, ("Nothing automatic.", "Review the signal."))
        if signal.name == "superseded_running":
            did = f"Cancelled {cancelled} superseded runs." if acted and cancelled else "Proposed cancelling the superseded runs."
        out.append(
            Alert(
                f"waste_{signal.name}",
                "normal",
                f"CI waste: {signal.name.replace('_', ' ')} is {signal.minutes:.0f} min ({signal.share:.0%}) over {report.window_days} days ({signal.detail}).",
                did,
                decide,
                f"waste:{signal.name}:{day}",
            )
        )
    return out


def gather(
    client: CIClient,
    config: CIHealthConfig,
    *,
    repos: tuple[str, ...],
    state_dir: Path,
    now: datetime | None = None,
    read_logs: bool = True,
) -> CIHealthReport:
    """Read only. Nothing is cancelled and nothing is sent."""
    now = now or datetime.now(UTC)
    org = default_org(config, repos)
    state = _budget.load_state(state_dir)
    report = CIHealthReport(
        checked_at=now.isoformat(),
        org=org,
        repos=tuple(repos),
        window_days=config.window_days,
        spend=None,
        budget_state=_budget.UNKNOWN,
        previous_state=str(state.get("budget_state") or _budget.UNKNOWN),
        run_minutes=0.0,
    )
    if org:
        spend = read_spend(client, config, org, now=now)
        report.spend = spend
        if spend.error:
            report.errors.append(f"billing: {spend.error}")
        else:
            report.budget_state = _budget.budget_state(spend.pct, config)
    else:
        report.errors.append("billing: no org; set ci_health.org or name a repo")
    since = (now - timedelta(days=config.window_days)).date().isoformat()
    runs: list[Run] = []
    for repo in repos:
        try:
            runs.extend(parse_runs(client.runs(repo, created_since=since, max_pages=config.max_run_pages), repo, now=now))
        except CIClientError as exc:
            report.errors.append(f"runs {repo}: {exc}")
    total = sum(r.minutes for r in runs)
    report.run_minutes = total
    report.runs_read = len(runs)
    report.by_repo = detect.minutes_by(runs, "repo")
    report.by_workflow = detect.minutes_by(runs, "workflow")
    report.by_event = detect.minutes_by(runs, "event")
    cache = state.get("selection_cache") if isinstance(state.get("selection_cache"), dict) else {}
    samples, jobs = _selection_samples(client, runs, config, cache) if read_logs else ([], [])
    report.by_job = detect.minutes_by_job(jobs)
    report.cancels = plan_cancels(runs, config, client)
    report.signals = [
        detect.update_branch_share(runs, config),
        detect.superseded_signal(report.cancels, runs, config),
        detect.selection_fallbacks(samples, config, total),
        detect.flakes(runs, config),
        detect.chronic_red(runs, config),
        detect.duplicate_main_runs(runs, config),
        detect.main_push_share(runs, config),
        detect.failed_minutes(runs, config),
    ]
    for repo in repos:
        report.lint.extend(
            lint_protection(
                client.branch_protection(repo, config.default_branch),
                client.branch_rules(repo, config.default_branch),
                branch=f"{repo}@{config.default_branch}",
            )
        )
    failures = state.get("main_failures") if isinstance(state.get("main_failures"), dict) else {}
    since_escape = (now - timedelta(days=config.escape_window_days)).date().isoformat()
    for repo in repos:
        try:
            long_runs = (
                runs
                if config.escape_window_days <= config.window_days
                else parse_runs(client.runs(repo, created_since=since_escape, max_pages=config.max_run_pages), repo, now=now)
            )
            repo_runs = [r for r in long_runs if r.repo == repo]
            pulls = escape.load_pulls(client, config, repo, now)
            report.escape[repo] = escape.escape_report(
                client, config, repo=repo, runs=repo_runs, pulls=pulls, cache=cache, failures=failures
            )
            report.culprits.extend(escape.find_culprits(client, config, runs=repo_runs, pulls=pulls, failures=failures))
        except CIClientError as exc:
            report.errors.append(f"escape {repo}: {exc}")
    report.failure_cache = dict(list(failures.items())[-200:])
    report.selection_cache = dict(list(cache.items())[-500:])
    return report


def sweep(
    client: CIClient,
    config: CIHealthConfig,
    *,
    repos: tuple[str, ...],
    state_dir: Path,
    act: bool = False,
    notify: bool = False,
    sender: Sender | None = None,
    now: datetime | None = None,
) -> CIHealthReport:
    """Gather, cancel superseded runs when ``act``, record state, and alert."""
    report = gather(client, config, repos=repos, state_dir=state_dir, now=now)
    report.cancel_results = apply_cancels(report.cancels, client, state_dir=state_dir, act=act)
    state = _budget.load_state(state_dir)
    reverts = dict(state.get("reverts") or {})
    for culprit in report.culprits:
        escape.draft_revert(client, config, culprit, already=reverts, act=act)
    move = _budget.transition(report.previous_state, report.budget_state)
    report.alerts = _alerts(report, config, move, act)
    sent_keys = set(state.get("sent") or [])
    if notify:
        report.sent = send_alerts(report.alerts, config.notify_recipients, already_sent=sent_keys, sender=sender)
    _budget.save_state(
        state_dir,
        {
            "checked_at": report.checked_at,
            "budget_state": report.budget_state,
            "pct": report.spend.pct if report.spend else None,
            "policy": _budget.landing_policy(report.budget_state, config).to_dict(),
            "sent": sorted(sent_keys)[-200:],
            "selection_cache": report.selection_cache,
            "main_failures": report.failure_cache,
            "reverts": reverts,
        },
    )
    return report


def _money(value: float | None) -> str:
    return "?" if value is None else f"${value:,.2f}"


def render_text(report: CIHealthReport, *, top: int = 5) -> str:
    lines: list[str] = []
    s = report.spend
    if s and not s.error:
        pct = f"{s.pct:.0f}%" if s.pct is not None else "no budget"
        lines.append(
            f"Actions {s.year}-{s.month:02d} to day {s.day}: {s.minutes:,.0f} min, gross {_money(s.gross)}, "
            f"net {_money(s.net)}; {pct} of {_money(s.budget)} ({report.budget_state})."
        )
        rr = f" ({s.run_rate_multiple:.1f}x last month {_money(s.last_month)})" if s.run_rate_multiple else ""
        proj_pct = f", {s.projected_pct:.0f}% of budget" if s.projected_pct is not None else ""
        lines.append(f"Projected month: {_money(s.projected)}{proj_pct}{rr}.")
        top_repos = sorted(s.by_repo.items(), key=lambda kv: -kv[1]["minutes"])[:top]
        if top_repos:
            lines.append("Billed minutes by repo: " + ", ".join(f"{k} {v['minutes']:,.0f}" for k, v in top_repos))
    lines.append(
        f"Run minutes, last {report.window_days} days (wall clock, {report.runs_read} runs read): {report.run_minutes:,.0f}"
    )
    for title, table in (("workflow", report.by_workflow), ("event", report.by_event), ("job (sampled)", report.by_job)):
        if table:
            lines.append(f"  by {title}: " + ", ".join(f"{k} {v:,.0f}" for k, v in list(table.items())[:top]))
    lines.append("Waste signals:")
    for signal in report.signals:
        mark = "!" if signal.over else "-"
        lines.append(f"  {mark} {signal.name}: {signal.count} runs, {signal.minutes:,.0f} min ({signal.share:.0%}). {signal.detail}")
    proposed = [d for d in report.cancels if d.cancel]
    if proposed:
        lines.append(f"Superseded runs to cancel: {len(proposed)}")
        for d in proposed[:top]:
            lines.append(f"  run {d.run.id} {d.run.workflow} on {d.run.head_branch}: {d.reason}")
    if report.cancel_results:
        done = sum(1 for r in report.cancel_results if r.get("outcome") == "cancelled")
        lines.append(f"Cancelled: {done} of {len(report.cancel_results)}")
    for repo, found in report.escape.items():
        rate = "no narrowed merges" if found.rate is None else f"{found.rate:.1%} of {found.narrowed_merges} narrowed merges"
        lines.append(
            f"Escape rate {repo}, {found.window_days} days: {len(found.escapes)} escaped ({found.per_30_days:.1f} per 30 days), "
            f"{rate}, {found.merges} merges, {found.unread_merges} unread (warn at {found.rate_warn:.0%})."
        )
        for item in found.escapes[:top]:
            lines.append(
                f"  escape PR #{item.pull.number} ({item.pull.title}): {', '.join(item.tests[:5]) or 'tests unknown'}; "
                f"changed types {', '.join(item.file_types)}"
            )
        for line in found.recommendations:
            lines.append(f"  propose: {line}")
    for culprit in report.culprits:
        pull = culprit.pull
        who = f"PR #{pull.number} by @{pull.author}" if pull else (f"PR #{culprit.pr_number}" if culprit.pr_number else "no PR found")
        state = "still red" if culprit.open else "recovered"
        extra = f"; {culprit.revert_url or culprit.revert_note}" if (culprit.revert_url or culprit.revert_note) else ""
        lines.append(
            f"Red main since {culprit.since} ({state}): {culprit.run.head_sha[:10]}, {who}; "
            f"failing {', '.join(culprit.tests[:5]) or 'unknown'}{extra}"
        )
    for finding in report.lint:
        lines.append(f"Lint: {finding.line()}")
    for alert in report.alerts:
        lines.append(f"ALERT {alert.kind}: {alert.summary} | RIVET did: {alert.action_taken} | You decide: {alert.decision}")
    for error in report.errors:
        lines.append(f"error: {error}")
    return "\n".join(lines) + "\n"


def render_digest(report: CIHealthReport) -> str:
    """The weekly digest: the same reading, worst signals first."""
    head = "RIVET weekly CI digest\n\n"
    ranked = sorted(report.signals, key=lambda s: -s.minutes)
    body = render_text(report)
    worst = "\n".join(
        f"- {s.name}: {s.minutes:,.0f} min. {PLAYBOOK.get(s.name, ('', ''))[1]}" for s in ranked[:3] if s.minutes > 0
    )
    return head + (f"Top waste this window:\n{worst}\n\n" if worst else "") + body


def to_json(report: CIHealthReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)
