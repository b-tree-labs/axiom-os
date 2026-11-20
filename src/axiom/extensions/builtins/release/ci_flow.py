# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What Rivet does with a red check, and who reviews the change.

A mechanical failure has a known repair and a resubmit. Anything else is
named and left for a person. A review is never done by the model that wrote
the change. These functions do not call a model and do not push. A harness
that has the Axiom MCP tools runs the repair and the review itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from axiom.extensions.builtins.release.ci_drift import (
    attribute_failure,
    blocked_pr_comment,
    coalesce,
    decide_landing,
    detect_toolchain,
    fyi_note,
    render_commands,
    version_changes,
)
from axiom.extensions.builtins.release.repair_catalog import CatalogEntry, match_entry


#: How many mechanical repairs the lander may name for one head. The next
#: failure after the cap is a person's decision, even when the log matches
#: a known repair.
REPAIR_TRY_CAP = 3

#: The patterns Rivet had before the catalog. Without facts, their plans
#: are exactly what they always were.
_LEGACY_IDS = frozenset({"context-sync", "ruff-fix", "short-run", "stray-root", "customer-name"})


def plan_repair(
    log: str,
    check: str = "",
    *,
    attempts: int = 0,
    facts: Mapping[str, Any] | None = None,
    catalog: Iterable[CatalogEntry] | None = None,
) -> dict[str, Any]:
    """The repair for a failure Rivet has already seen, or an escalation.

    ``resubmit`` is never ``update-branch``. Updating a branch through the
    host's API skips the pre-push hook, which is how a repairable failure
    lands on GitHub again. ``attempts`` counts repairs already tried on
    this head; at ``REPAIR_TRY_CAP`` the plan stops.

    The failure is matched against :data:`repair_catalog.CATALOG` (or
    ``catalog``). Without ``facts`` a known pattern returns the same six
    keys it always did. With ``facts`` (see :func:`_drift_plan`) the plan
    also says who caused the failure, which toolchain to pin, where the
    repair goes, whether to push, and what the owners are told.
    """
    if attempts >= REPAIR_TRY_CAP:
        stopped = _stop(
            "try-cap",
            f"Mechanical repair already tried {attempts} times "
            f"(cap {REPAIR_TRY_CAP}). A person decides the next step.",
        )
        stopped["escalate"] = True
        return stopped
    text = f"{check} {log}"
    entry = match_entry(text, catalog)
    if entry is None:
        return _stop("unknown", "No mechanical repair is known for this failure.")
    plan = _plan(entry.id, entry.detail, list(entry.commands), entry.resubmit)
    if not entry.commands and entry.resubmit == "none":
        plan["escalate"] = entry.escalate
    if entry.status == "active" and entry.id in _LEGACY_IDS and not facts:
        return plan
    return _drift_plan(entry, plan, log, check, dict(facts or {}))


def _drift_plan(
    entry: CatalogEntry,
    plan: dict[str, Any],
    log: str,
    check: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """The richer plan. Every key of the old plan keeps its meaning.

    Facts the harness may supply: ``pr``, ``pr_files``, ``check_globs``,
    ``base_branch``, ``base_result``, ``base_result_at``,
    ``upstream_release_at``, ``workflow``, ``job``, ``branch_pushable``,
    ``in_flight`` (repairs other pull requests are already carrying, as
    ``{key, pr}``), and after the repair ran, ``diff_stats``,
    ``verify_result``, ``codeowners``, ``recent_authors`` and ``actor``.
    """
    base = str(facts.get("base_branch") or "main")
    pr = str(facts.get("pr") or "")
    plan.update(
        {
            "catalog_entry": entry.to_dict(),
            "verify": "",
            "fix_on": "pr_branch",
            "steps": [],
            "escalate_reasons": [],
        }
    )
    if entry.escalate or not entry.commands:
        if entry.escalate:
            plan["escalate"] = True
            plan["escalate_reasons"] = list(entry.stop_conditions)
        return plan

    attribution = attribute_failure(
        entry_inputs=entry.inputs,
        pr_files=facts.get("pr_files"),
        check_globs=facts.get("check_globs"),
        base_result=str(facts.get("base_result") or ""),
        base_result_at=str(facts.get("base_result_at") or ""),
        upstream_release_at=str(facts.get("upstream_release_at") or ""),
        inputs_complete=entry.inputs_complete,
    )
    plan["attribution"] = attribution
    verdict = attribution["verdict"]
    drift = verdict == "base_drift"

    toolchain: dict[str, Any] = {}
    commands = list(entry.commands)
    verify = entry.verify
    if entry.toolchain:
        toolchain = detect_toolchain(str(facts.get("workflow") or ""), str(facts.get("job") or check))
        plan["toolchain"] = toolchain
        if not toolchain["found"]:
            plan.update(
                {
                    "detail": (
                        f"{entry.detail} The repair needs CI's {entry.toolchain} toolchain "
                        "pinned first: supply the workflow text and the job."
                    ),
                    "commands": [],
                    "resubmit": "none",
                    "escalate": False,
                    "needs_facts": ["workflow", "job"],
                }
            )
            return plan
        commands = render_commands(entry.commands, toolchain)
        verify = render_commands([entry.verify], toolchain)[0] if entry.verify else ""
    plan["commands"] = commands
    plan["verify"] = verify

    changes = list(facts.get("version_changes") or version_changes(log))
    target = ",".join(sorted(f"{c['package']}@{c['expected']}" for c in changes))
    key = f"{entry.id}:{base}:{toolchain.get('working_directory') or '.'}"
    if target:
        key = f"{key}:{target}"
    plan["drift_key"] = key
    pushable = facts.get("branch_pushable", True)
    pushable = str(pushable).strip().lower() not in {"false", "0", "no", ""}
    if drift:
        waiting = coalesce(key, pr, facts.get("in_flight") or ())
        if waiting["action"] == "wait":
            plan.update(
                {
                    "fix_on": "none",
                    "commands": [],
                    "resubmit": "after-base-fix",
                    "waiting_on": waiting["waiting_on"],
                    "detail": (
                        f"{entry.detail} Pull request {waiting['waiting_on']} already carries "
                        f"the repair for {key}. This one waits for it."
                    ),
                    "steps": [
                        f"Wait for pull request {waiting['waiting_on']} to land on {base}.",
                        *_bring_base_in(base),
                    ],
                    "blocked_pr_comment": blocked_pr_comment(
                        check=check or entry.id,
                        base=base,
                        detail=entry.detail,
                        evidence=attribution["evidence"],
                    ),
                }
            )
            return plan
    if not pushable:
        if not drift:
            plan["escalate"] = True
            plan["escalate_reasons"] = ["the pull request's branch cannot take pushes"]
            plan["resubmit"] = "none"
            return plan
        plan["fix_on"] = "repair_pr"
        plan["repair_pr"] = {
            "base": base,
            "branch": f"rivet/base-drift/{entry.id}",
            "auto_merge": True,
            "one_per_key": key,
            "close_if_superseded": True,
        }

    label = "base-drift repair" if drift else "repair"
    plan["commit_message"] = f"rivet: {label} — {entry.id}"
    plan["resubmit"] = "push"
    carried_by = (
        f"pull request {pr}" if pr and plan["fix_on"] == "pr_branch" else (
            f"branch rivet/base-drift/{entry.id}" if plan["fix_on"] == "repair_pr" else "this pull request"
        )
    )
    pin = toolchain.get("pin") or ""
    steps = [
        f"Run the repair from the repository root on {carried_by}"
        + (f" under {pin} (CI's toolchain)." if pin else "."),
        "Run the verify command. If it fails, stop and escalate.",
        f"Commit as '{plan['commit_message']}' and push through the pre-push hook.",
    ]
    if drift:
        steps.append("Comment on the pull request: base drift, not caused by this pull request.")
        steps.append("Leave the FYI note for the owners of the touched paths.")
        steps.append(
            f"Other pull requests blocked by {key} wait for this repair; once it is on "
            f"{base} each brings {base} in through the pre-push path and re-runs."
        )
        plan["blocked_pr_comment"] = blocked_pr_comment(
            check=check or entry.id, base=base, detail=entry.detail, evidence=attribution["evidence"]
        )
        plan["after_landing"] = _bring_base_in(base)
    plan["steps"] = steps

    plan["version_changes"] = changes
    landing = decide_landing(
        diff_stats=facts.get("diff_stats"),
        verify_result=str(facts.get("verify_result") or ""),
        budget=entry.churn_budget,
        changes=changes,
        breaking=entry.breaking,
        actor=str(facts.get("actor") or ""),
        codeowners=str(facts.get("codeowners") or ""),
        recent_authors=facts.get("recent_authors") or None,
    )
    plan["guard"] = landing
    if landing["decision"] == "escalate":
        plan.update({"escalate": True, "escalate_reasons": landing["reasons"], "resubmit": "none"})
        return plan
    if landing["decision"] == "stop":
        plan.update({"escalate": True, "escalate_reasons": landing["reasons"], "resubmit": "none"})
        return plan
    plan["escalate"] = False
    if drift and landing["notify"]:
        plan["owner_note"] = fyi_note(
            check=check or entry.id,
            base=base,
            entry_id=entry.id,
            detail=entry.detail,
            commands=list(entry.commands),
            verify=entry.verify,
            toolchain_pin=pin,
            churn=landing["churn"],
            owners=landing["notify"],
            carried_by=carried_by,
        )
    return plan


def _bring_base_in(base: str) -> list[str]:
    return [
        f"git fetch origin {base}",
        f"git merge --no-edit origin/{base}",
        "git push  # through the pre-push hook; never the host's update-branch",
        "re-run the failed check",
    ]


def choose_reviewer(author_model: str, models: list[str]) -> dict[str, Any]:
    """A review model that is not the one that wrote the change."""
    author = (author_model or "").strip()
    roster = []
    for model in models:
        name = (model or "").strip()
        if name and name not in roster:
            roster.append(name)
    others = [name for name in roster if name != author]
    if not author:
        return {"ok": False, "reviewer": "", "reason": "name the model that wrote the change"}
    if not others:
        return {
            "ok": False,
            "reviewer": "",
            "reason": f"the review needs a model other than {author}",
        }
    return {"ok": True, "reviewer": others[0], "author_model": author, "reason": ""}


def independent_reviewers(
    author_model: str,
    models: list[str],
    *,
    count: int = 2,
) -> dict[str, Any]:
    """``count`` reviewers, each chosen by :func:`choose_reviewer`.

    Every pick is someone other than the author. The next pick is made
    from the roster that remains, so the two reviews are independent.
    """
    picked: list[str] = []
    remaining = list(models)
    reason = ""
    needed = max(1, count)
    for _ in range(needed):
        choice = choose_reviewer(author_model, remaining)
        if not choice.get("ok"):
            reason = str(choice.get("reason") or "")
            break
        name = str(choice.get("reviewer") or "")
        if not name:
            reason = "the review needs a named model"
            break
        picked.append(name)
        remaining = [item for item in remaining if item != name]
    return {
        "ok": len(picked) == needed,
        "reviewers": picked,
        "author_model": (author_model or "").strip(),
        "reason": reason,
    }


def _plan(pattern: str, detail: str, commands: list[str], resubmit: str) -> dict[str, Any]:
    return {
        "ok": True,
        "pattern": pattern,
        "detail": detail,
        "commands": commands,
        "resubmit": resubmit,
        "escalate": False,
    }


def _stop(pattern: str, detail: str) -> dict[str, Any]:
    return {
        "ok": True,
        "pattern": pattern,
        "detail": detail,
        "commands": [],
        "resubmit": "none",
        "escalate": pattern == "unknown",
    }


__all__ = ["REPAIR_TRY_CAP", "choose_reviewer", "independent_reviewers", "plan_repair"]
