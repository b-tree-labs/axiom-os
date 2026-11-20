# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One lander pass, from the heartbeat or from ``axi release land --propose``.

Order is batch, review, fix, propose. The pass takes the switchplan file
lease for the repo, writes a switchplan record and a JSON-lines event,
and claims the pull requests it would act on so the auto-closer skips
them. It does not merge, enqueue, force-push, or post a check.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.release.ci_flow import choose_reviewer, plan_repair
from axiom.extensions.builtins.release.lander.adapters import GitHubForge
from axiom.extensions.builtins.release.lander.config import LanderConfig, load_config, watch_repos
from axiom.extensions.builtins.release.lander.coordination import claim, held_by, replace_actor_claims
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.report import BacklogReport, build_report
from axiom.extensions.builtins.release.lander.status import advisory_status, assert_advisory
from axiom.infra.state import locked_append_jsonl
from axiom.infra.switchplan.ledger import FileLedger, new_record
from axiom.infra.switchplan.lock import SingleFlight

STEPS = ("batch", "review", "fix", "propose")


def flight_name(repo: str) -> str:
    return f"release.lander:{repo}"


@dataclass
class LoopResult:
    action: str
    repo: str
    report: BacklogReport | None = None
    reason: str = ""
    steps: tuple[str, ...] = ()
    status_posted: bool = False
    merged: bool = False
    enqueued: bool = False

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "repo": self.repo,
            "reason": self.reason,
            "steps": list(self.steps),
            "status_posted": self.status_posted,
            "merged": self.merged,
            "enqueued": self.enqueued,
            "report": None if self.report is None else self.report.to_dict(),
        }


def load_approvals(state_dir: Path, repo: str) -> dict[int, str]:
    path = Path(state_dir) / "agents" / "rivet" / "lander-approvals.json"
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[int, str] = {}
    prefix = f"{repo}#"
    for key, value in data.items():
        if not str(key).startswith(prefix) or not isinstance(value, dict):
            continue
        number = str(key)[len(prefix) :]
        sha = value.get("head_sha")
        if number.isdigit() and isinstance(sha, str):
            found[int(number)] = sha
    return found


def note_approval(state_dir: Path, *, repo: str, number: int, head_sha: str, by: str) -> None:
    """Bind an approval to one head. A later push (a different sha) resets it.

    This writes local state only. It does not merge.
    """
    import json

    path = Path(state_dir) / "agents" / "rivet" / "lander-approvals.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    from datetime import UTC, datetime

    data[f"{repo}#{number}"] = {
        "head_sha": head_sha,
        "by": by,
        "at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


def _signals(report: BacklogReport) -> list[dict]:
    """Same category the failure store uses. A capped repair routes to a person."""
    signals = []
    for item in report.needs_human:
        if "try-cap" in item.reasons:
            signals.append(
                {
                    "category": "ci_failure",
                    "repo": report.repo,
                    "pr": item.number,
                    "failure_reason": "try-cap",
                    "next_route": "human",
                }
            )
    return signals


def _write_ledger(state_dir: Path, report: BacklogReport) -> None:
    directory = Path(state_dir) / "agents" / "rivet" / "lander-records"
    ledger = FileLedger(directory)
    record = new_record("release.lander", report.repo)
    record.phase = "planned"
    record.state = {
        "mode": report.mode,
        "minutes_saved": report.minutes_saved,
        "ready": [item.number for item in report.ready],
        "superseded": [item.number for item in report.superseded],
    }
    record.event("propose", f"{report.open_count} open, save about {report.minutes_saved} minutes")
    ledger.save(record)
    locked_append_jsonl(
        Path(state_dir) / "agents" / "rivet" / "lander.jsonl",
        {
            "action": "propose",
            "repo": report.repo,
            "record_id": record.id,
            "mode": report.mode,
            "steps": list(STEPS),
            "ready": [item.number for item in report.ready],
            "superseded": [item.number for item in report.superseded],
            "needs_human": [item.number for item in report.needs_human],
            "batches": [batch.key for batch in report.batches],
            "minutes_saved": report.minutes_saved,
            "signals": _signals(report),
            "status_posted": False,
            "merged": False,
            "enqueued": False,
        },
    )


def _review_and_fix(report: BacklogReport, config: LanderConfig) -> None:
    """Call the review and repair skills so the pass really does those steps.

    The skills name a reviewer and a repair. They do not push.
    """
    if config.review_roster:
        choose_reviewer("", list(config.review_roster))
    for item in report.needs_human:
        if item.repair:
            continue
        if "failing_checks" in item.reasons or "try-cap" in item.reasons:
            plan_repair(item.title, attempts=config.repair_cap if "try-cap" in item.reasons else 0)


def run_once(
    *,
    repo: str,
    state_dir: Path,
    config: LanderConfig | None = None,
    pulls: list[OpenPull] | None = None,
    forge: GitHubForge | None = None,
    samples: list[int] | None = None,
) -> LoopResult:
    """Propose for one repo. A second caller while the lease is held is skipped."""
    config = config or load_config(state_dir, repo)
    if config.mode not in {"warn", "propose"}:
        raise LanderRefused(f"unknown lander mode {config.mode!r}")
    # The advisory payload is built and checked. Nothing posts it.
    assert_advisory(advisory_status("warn", []))
    lease = Path(state_dir) / "agents" / "rivet" / "lander-leases"
    flight = SingleFlight(flight_name(repo), lease_dir=lease)
    if not flight.acquire():
        return LoopResult(action="skip", repo=repo, reason="busy")
    try:
        if pulls is None:
            forge = forge or GitHubForge(limit=config.pr_limit, self_landers=config.self_landers)
            pulls = forge.list_open(repo)
            if samples is None and config.suite_minutes is None:
                try:
                    samples = forge.suite_samples(repo)
                except Exception:
                    samples = []
        claimed = [(repo, pull.number, pull.head_sha) for pull in pulls]
        replace_actor_claims(state_dir, "lander", claimed, ttl_s=config.claim_ttl_s)
        report = build_report(
            pulls,
            repo=repo,
            config=config,
            samples=samples,
            approvals=load_approvals(state_dir, repo),
            state_dir=state_dir,
        )
        actionable = [(repo, item.number, item.head_sha) for item in report.ready]
        actionable.extend(
            (repo, item.number, "")
            for item in report.superseded
            if not held_by(state_dir, item.number, actor="auto_closer", repo=repo)
        )
        # Drop anything the closer took while we were reading.
        actionable = [
            item
            for item in actionable
            if item[1] not in set(report.blocked_by_closer)
            and claim(
                state_dir,
                repo=repo,
                number=item[1],
                actor="lander",
                head_sha=item[2],
                ttl_s=config.claim_ttl_s,
            )
        ]
        replace_actor_claims(state_dir, "lander", actionable, ttl_s=config.claim_ttl_s)
        _review_and_fix(report, config)
        _write_ledger(state_dir, report)
        return LoopResult(
            action="propose",
            repo=repo,
            report=report,
            steps=STEPS,
            status_posted=False,
            merged=False,
            enqueued=False,
        )
    finally:
        flight.release()


def heartbeat_propose(state_dir: Path) -> dict:
    """Heartbeat hook. No configured repos means no forge calls and no writes."""
    repos = watch_repos(state_dir)
    if not repos:
        return {"ran": False, "reason": "no_repos", "mode": "warn"}
    summaries = []
    for repo in repos:
        try:
            result = run_once(repo=repo, state_dir=Path(state_dir))
            summaries.append(
                {
                    "repo": repo,
                    "action": result.action,
                    "reason": result.reason,
                    "ready": 0 if result.report is None else len(result.report.ready),
                    "batches": 0 if result.report is None else len(result.report.batches),
                    "minutes_saved": 0 if result.report is None else result.report.minutes_saved,
                }
            )
        except Exception as exc:
            summaries.append({"repo": repo, "action": "skip", "reason": type(exc).__name__})
    return {"ran": True, "mode": "warn", "repos": summaries}
