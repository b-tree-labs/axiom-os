# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The lander's base-drift path: gather the facts, repair once, coalesce.

When a pull request's check is red with a failure the repair catalog knows
as drift, the lander does the whole job itself:

1. Gathers the facts ``plan_repair`` needs from the host: the pull
   request's files, the failing run's log, workflow file and job, the
   base branch's latest result for that job, CODEOWNERS, and the repairs
   already in flight (the drift ledger).
2. Plans. Base drift rides the blocked pull request as its own labelled
   commit. Another pull request already carrying the same repair means
   this one waits. A carrier that landed means this one brings the base in
   through a plain push (the pre-push hook runs) and re-runs its check.
3. Runs the repair and its verify in CI's image when a container runtime
   is present, or locally only when local tools match CI's pinned ones.
4. Pushes a verified repair, comments on the pull request (base drift, not
   caused by it, and an FYI for the owners of the touched paths), and
   records the repair in the ledger. A failing verify, a major version
   change, a sensitive path or the try cap escalates instead.

The path runs only when the repository's lander config allows it
(``raci.auto_allowed`` includes ``drift_repair``). Without that, or for a
failure that is not drift, :func:`resolve_drift` returns ``None`` and the
lander does what it always did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from axiom.extensions.builtins.release.ci_drift import (
    choose_execution,
    render_commands,
    workflow_paths,
)
from axiom.extensions.builtins.release.ci_flow import _LEGACY_IDS, REPAIR_TRY_CAP, plan_repair
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.drift_ledger import ACTIVE, DriftLedger
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.protect import config_categories
from axiom.extensions.builtins.release.repair_catalog import match_entry

ACTION = "drift_repair"
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


class DriftHost(Protocol):
    """What the lander reads from and writes to the forge for a drift repair."""

    def pr_files(self, number: int) -> list[str]: ...

    def failing_run(self, number: int, check: str) -> dict | None:
        """``{log, workflow_path, job, run_id}`` for the pull request's red ``check``."""

    def file_at(self, ref: str, path: str) -> str: ...

    def base_check(self, base: str, workflow_path: str, job: str) -> dict:
        """``{result, at}``: the base branch's latest completed result for the job."""

    def branch_pushable(self, number: int) -> bool: ...

    def comment(self, number: int, body: str) -> None: ...

    def pr_state(self, number: int) -> dict: ...

    def rerun(self, run_id: str) -> None: ...

    def open_pr(self, *, base: str, head: str, title: str, body: str, auto_merge: bool) -> int: ...


class Workspace(Protocol):
    """A checkout the lander may change. Pushes are plain and run the pre-push hook."""

    def docker_available(self) -> bool: ...

    def tool_versions(self) -> dict: ...

    def checkout(self, ref: str) -> None: ...

    def branch_from(self, base: str, branch: str) -> None: ...

    def run(self, command: str) -> tuple[int, str]: ...

    def numstat(self) -> str: ...

    def discard(self) -> None: ...

    def commit(self, message: str) -> str: ...

    def push(self, ref: str) -> str: ...

    def merge_base(self, base: str) -> str: ...


@dataclass
class DriftWiring:
    host: DriftHost
    workspace: Workspace
    actor: str = "rivet"


@dataclass
class DriftOutcome:
    status: str
    reason: str = ""
    head_sha: str = ""
    steps: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)


def enabled(config: LanderConfig) -> bool:
    return ACTION in config.auto_allowed


def resolve_drift(
    pull: OpenPull,
    *,
    config: LanderConfig,
    wiring: DriftWiring,
    ledger: DriftLedger,
) -> DriftOutcome | None:
    """Resolve a red pull request whose failure is drift, or ``None``."""
    if not enabled(config) or pull.checks != "failing":
        return None
    checks = [check.name for check in pull.failing_checks] or [""]
    for check in checks:
        run = wiring.host.failing_run(pull.number, check) or {}
        log = str(run.get("log") or pull.failure_log or "")
        entry = match_entry(f"{check} {log}")
        if entry is None or entry.id in _LEGACY_IDS:
            continue
        return _resolve(pull, check, log, run, config=config, wiring=wiring, ledger=ledger)
    return None


def _facts(pull: OpenPull, run: dict, check: str, wiring: DriftWiring, ledger: DriftLedger) -> dict[str, Any]:
    host = wiring.host
    base = pull.base_ref or "main"
    path = str(run.get("workflow_path") or "")
    job = str(run.get("job") or check)
    workflow = host.file_at(pull.head_ref or base, path) if path else ""
    base_result = host.base_check(base, path, job) if path else {}
    globs = workflow_paths(workflow)
    return {
        "pr": str(pull.number),
        "pr_files": list(host.pr_files(pull.number) or pull.files),
        "check_globs": globs or None,
        "base_branch": base,
        "base_result": str((base_result or {}).get("result") or ""),
        "base_result_at": str((base_result or {}).get("at") or ""),
        "workflow": workflow,
        "job": job,
        "branch_pushable": host.branch_pushable(pull.number),
        "in_flight": ledger.in_flight(),
    }


def _codeowners(host: DriftHost, base: str) -> str:
    for path in CODEOWNERS_PATHS:
        text = host.file_at(base, path)
        if text:
            return text
    return ""


def _escalate(reason: str, steps: list[str], plan: dict | None = None, sha: str = "") -> DriftOutcome:
    return DriftOutcome("escalated", reason[:300], sha, tuple(steps + ["escalate"]), plan or {})


def _resolve(
    pull: OpenPull,
    check: str,
    log: str,
    run: dict,
    *,
    config: LanderConfig,
    wiring: DriftWiring,
    ledger: DriftLedger,
) -> DriftOutcome:
    host, workspace = wiring.host, wiring.workspace
    steps = ["drift:facts"]
    facts = _facts(pull, run, check, wiring, ledger)
    base = facts["base_branch"]
    plan = plan_repair(log, check, facts=facts)
    steps.append("drift:plan")
    if plan.get("escalate"):
        reasons = plan.get("escalate_reasons") or [plan.get("detail") or plan.get("pattern")]
        return _escalate(f"{plan['pattern']}: " + "; ".join(map(str, reasons)), steps, plan)
    if plan.get("needs_facts"):
        notes = "; ".join((plan.get("toolchain") or {}).get("notes") or [])
        return _escalate(
            "toolchain-unknown: CI's toolchain could not be read from the workflow"
            + (f" ({notes})" if notes else ""),
            steps,
            plan,
        )
    key = str(plan.get("drift_key") or "")
    existing = ledger.get(key) or {}
    carrier = str(existing.get("pr") or "")
    status = str(existing.get("status") or "")
    mine = carrier == str(pull.number)

    if status == "landed" and not mine:
        return _bring_base_in(pull, run, base, key, plan, wiring, ledger, steps)
    if status in ACTIVE and mine:
        return DriftOutcome(
            "waiting", f"repair {existing.get('commit', '')} pushed; waiting for CI", pull.head_sha, tuple(steps), plan
        )
    if plan.get("waiting_on"):
        if ledger.tell_once(key, pull.number):
            host.comment(
                pull.number,
                f"{plan.get('blocked_pr_comment', '')}\n\nThe repair rides #{plan['waiting_on']}; "
                "this pull request takes it from the base and re-runs once that lands.",
            )
        return DriftOutcome("waiting", f"waiting on #{plan['waiting_on']} ({key})", pull.head_sha, tuple(steps), plan)

    attempts = int(existing.get("attempts") or 0)
    if attempts >= REPAIR_TRY_CAP:
        capped = plan_repair(log, check, attempts=attempts, facts=facts)
        return _escalate(f"try-cap: {capped['detail']}", steps, capped)

    toolchain = plan.get("toolchain") or {}
    docker = workspace.docker_available()
    execution = choose_execution(toolchain, docker=docker, local={} if docker else workspace.tool_versions())
    if execution["mode"] == "escalate":
        return _escalate(f"toolchain: {execution['reason']}", steps, plan)
    raw = plan["catalog_entry"]
    if execution["mode"] == "container":
        commands, verify = list(plan["commands"]), str(plan.get("verify") or "")
    else:
        local = {**toolchain, "image": ""}
        commands = render_commands(raw["commands"], local)
        verify = render_commands([raw["verify"]], local)[0] if raw.get("verify") else ""

    repair_pr = plan.get("repair_pr") or {}
    ref = str(repair_pr.get("branch") or pull.head_ref)
    if repair_pr:
        workspace.branch_from(base, ref)
    else:
        workspace.checkout(ref)
    ledger.note_attempt(key, str(pull.number) if not repair_pr else f"branch:{ref}")
    steps.append(f"drift:run:{execution['mode']}")
    try:
        ok = all(workspace.run(command)[0] == 0 for command in commands)
        verified = ok and (not verify or workspace.run(verify)[0] == 0)
        diff = workspace.numstat()
    except Exception as exc:
        ledger.upsert(key, status="failed", reason=f"repair did not run: {exc}"[:300])
        raise
    steps.append("drift:verify")
    decided = plan_repair(
        log,
        check,
        attempts=attempts,
        facts={
            **facts,
            "diff_stats": diff,
            "verify_result": "pass" if verified else "fail",
            "codeowners": _codeowners(host, base),
            "actor": wiring.actor,
        },
    )
    guard = decided.get("guard") or {}
    reasons = list(guard.get("reasons") or [])
    protected = [
        category
        for category in config_categories(tuple(_paths(diff)), config)
        if category != "dependency"
    ]
    if protected:
        reasons.append("touches protected paths: " + ", ".join(protected))
    if guard.get("decision") != "push" or protected:
        workspace.discard()
        ledger.upsert(key, status="failed", reason="; ".join(reasons) or str(guard.get("decision")))
        return _escalate("; ".join(reasons) or f"repair {guard.get('decision')}", steps, decided)

    message = str(decided.get("commit_message") or f"rivet: repair — {raw['id']}")
    try:
        sha = workspace.commit(message)
        workspace.push(ref)
    except Exception as exc:
        # The signature must not stay "verifying", or every other pull request waits on it.
        ledger.upsert(key, status="failed", reason=f"push refused: {exc}"[:300])
        raise
    steps.append("drift:push")
    drift = (decided.get("attribution") or {}).get("verdict") == "base_drift"
    note = "\n\n".join(
        part for part in (decided.get("blocked_pr_comment", "") if drift else "", decided.get("owner_note", "")) if part
    ) or f"Rivet pushed `{message}`: {decided.get('detail', '')}"
    carrier_pr = str(pull.number)
    if repair_pr:
        number = host.open_pr(
            base=base,
            head=ref,
            title=message,
            body=f"{decided.get('detail', '')}\n\n{note}",
            auto_merge=True,
        )
        carrier_pr = str(number)
        steps.append("drift:repair-pr")
    ledger.upsert(
        key,
        pr=carrier_pr,
        status="pushed",
        commit=sha,
        check=check,
        run_id=str(run.get("run_id") or ""),
        churn=guard.get("churn"),
    )
    ledger.tell_once(key, pull.number)
    host.comment(pull.number, note if not repair_pr else f"{note}\n\nThe repair rides #{carrier_pr}.")
    return DriftOutcome("repaired", f"{raw['id']} on {ref}", sha, tuple(steps), decided)


def _paths(numstat: str) -> list[str]:
    return [line.split("\t")[2].strip() for line in numstat.splitlines() if line.count("\t") >= 2]


def _bring_base_in(
    pull: OpenPull,
    run: dict,
    base: str,
    key: str,
    plan: dict,
    wiring: DriftWiring,
    ledger: DriftLedger,
    steps: list[str],
) -> DriftOutcome:
    """The repair is on the base: merge it in with a plain push, then re-run."""
    host, workspace = wiring.host, wiring.workspace
    if not host.branch_pushable(pull.number):
        if ledger.tell_once(f"{key}#landed", pull.number):
            host.comment(
                pull.number,
                f"The repair for this check is on `{base}` now. This branch cannot take "
                f"pushes from Rivet; merging `{base}` into it will turn the check green.",
            )
        return DriftOutcome("waiting", f"{key} landed; branch cannot take pushes", pull.head_sha, tuple(steps), plan)
    workspace.checkout(pull.head_ref)
    sha = workspace.merge_base(base)
    workspace.push(pull.head_ref)
    run_id = str(run.get("run_id") or "")
    if run_id:
        host.rerun(run_id)
    steps.extend(["drift:merge-base", "drift:push", "drift:rerun"])
    return DriftOutcome("rebased", f"{key} landed on {base}; base merged in and re-run", sha, tuple(steps), plan)


__all__ = ["ACTION", "DriftHost", "DriftOutcome", "DriftWiring", "Workspace", "enabled", "resolve_drift"]
