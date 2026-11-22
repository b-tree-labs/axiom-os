# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``hygiene.artifacts`` / ``hygiene.storage_tick`` / ``hygiene.storage_digest``.

Inventory GitHub Actions artifacts and caches, apply TIDY's storage policy,
and *act* by default inside that policy. HERALD is notified of what was
deleted and why, with a recovery path. A kill switch, ``--dry-run``,
per-run caps, and a rate limit bound the agency. At 70% TIDY reclaims
in-policy; at 85% it reclaims aggressively (still inside policy) and
raises a Needs you item if storage is still over.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from axiom.extensions.builtins.hygiene.agency import (
    RECOVERY_ARTIFACT,
    RECOVERY_CACHE,
    apply_blast_caps,
    blast_caps,
    decide_act,
    file_needs_you,
    rate_limit_allows,
    record_enforce_run,
    storage_watch_only as storage_watch_only,
)
from axiom.extensions.builtins.hygiene.artifact_cleanup import (
    Artifact,
    CacheRecord,
    GitHubProvider,
    StoragePolicy,
    _pr_number,
)
from axiom.extensions.builtins.hygiene.storage_alerts import Alert, from_crossing, send_alerts
from axiom.extensions.builtins.hygiene.storage_audit import append_audit
from axiom.extensions.builtins.hygiene.storage_watch import (
    StorageReading,
    accrued_gb_hours_from_usage_summary,
    classify_thresholds,
)
from axiom.infra.skills import SkillContext, SkillResult

_log = logging.getLogger("axiom.hygiene.storage")

GhRunner = Callable[..., Any]


def _repos(params: dict[str, Any]) -> list[str]:
    raw = params.get("repo") or params.get("repos")
    if isinstance(raw, str) and raw.strip():
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(part) for part in raw]
    env = os.environ.get("AXIOM_ACTIONS_REPOS") or os.environ.get("AXIOM_STORAGE_REPOS") or ""
    if env.strip():
        return [part.strip() for part in env.split(",") if part.strip()]
    # Default: this checkout, if it is a GitHub repo.
    from axiom.extensions.builtins.release.actions_spend import watched_repos

    state = Path(params["state_dir"]) if params.get("state_dir") else None
    return list(watched_repos(state))


def _provider(params: dict[str, Any]) -> GitHubProvider:
    runner = params.get("_runner")
    return GitHubProvider(runner=runner) if runner is not None else GitHubProvider()


def _gh_json(runner: GhRunner, path: str) -> dict | list | None:
    result = runner(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _open_and_closed_prs(
    repos: list[str],
    caches: list[CacheRecord],
    runner: GhRunner,
) -> tuple[dict[str, set[int]], dict[tuple[str, int], bool]]:
    open_prs: dict[str, set[int]] = {repo: set() for repo in repos}
    closed: dict[tuple[str, int], bool] = {}
    needed: dict[str, set[int]] = {}
    for cache in caches:
        n = _pr_number(cache.ref)
        if n is not None:
            needed.setdefault(cache.repo, set()).add(n)
    for repo in repos:
        payload = _gh_json(runner, f"repos/{repo}/pulls?state=open&per_page=100")
        if isinstance(payload, list):
            for pr in payload:
                try:
                    open_prs[repo].add(int(pr["number"]))
                except (KeyError, TypeError, ValueError):
                    continue
        for n in needed.get(repo, set()):
            if n in open_prs[repo]:
                continue
            pr = _gh_json(runner, f"repos/{repo}/pulls/{n}")
            if isinstance(pr, dict):
                closed[(repo, n)] = (pr.get("state") == "closed") or bool(pr.get("merged_at"))
    return open_prs, closed


def _reading(
    artifacts: list[Artifact],
    caches: list[CacheRecord],
    params: dict[str, Any],
    runner: GhRunner,
) -> StorageReading:
    cache_by_repo: dict[str, int] = {}
    for cache in caches:
        cache_by_repo[cache.repo] = cache_by_repo.get(cache.repo, 0) + cache.size_bytes
    injected = params.get("_reading")
    if isinstance(injected, StorageReading):
        return injected
    org = params.get("org") or os.environ.get("AXIOM_STORAGE_ORG") or ""
    accrued = None
    if org:
        payload = _gh_json(runner, f"organizations/{org}/settings/billing/usage/summary")
        if isinstance(payload, dict):
            accrued = accrued_gb_hours_from_usage_summary(payload)
    return StorageReading(
        artifact_bytes=sum(a.size_bytes for a in artifacts),
        cache_bytes_by_repo=cache_by_repo,
        accrued_gb_hours=accrued,
    )


def _action_line(*, watch_only: bool, art_n: int, art_b: int, cache_n: int, cache_b: int) -> str:
    verb = "Would delete" if watch_only else "Deleted"
    return (
        f"{verb} {art_n} artifacts ({art_b / (1024**3):.3f} GiB) and "
        f"{cache_n} caches ({cache_b / (1024**3):.3f} GiB)"
    )


def _recipients(params: dict[str, Any]) -> tuple[str, ...]:
    recipients = params.get("recipients")
    if isinstance(recipients, str):
        return tuple(part.strip() for part in recipients.split(",") if part.strip())
    if isinstance(recipients, (list, tuple)):
        return tuple(str(r) for r in recipients)
    return ()


def run_storage(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Inventory, classify, and delete inside policy. Default is to act."""
    now = params.get("now") or datetime.now(timezone.utc)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    repos = _repos(params)
    if not repos:
        return SkillResult(ok=False, errors=["no repo: pass --repo owner/name or set AXIOM_ACTIONS_REPOS"])

    settings_get = params.get("_settings_get")
    if not params.get("surface") and getattr(ctx, "surface", None):
        params = dict(params)
        params["surface"] = ctx.surface
    act, act_reason = decide_act(params, settings_get=settings_get)
    watch_only = not act

    provider = params.get("_provider") or _provider(params)
    runner = params.get("_runner") or subprocess.run
    policy: StoragePolicy = params.get("policy") or StoragePolicy.from_settings(settings_get)

    artifacts: list[Artifact] = []
    caches: list[CacheRecord] = []
    for repo in repos:
        artifacts.extend(list(provider.list_artifacts(repo)))
        if hasattr(provider, "list_caches"):
            caches.extend(list(provider.list_caches(repo)))

    open_prs, closed = _open_and_closed_prs(repos, caches, runner)
    if params.get("_open_prs") is not None:
        open_prs = params["_open_prs"]
    if params.get("_closed_or_merged") is not None:
        closed = params["_closed_or_merged"]

    from axiom.infra.artifact_verdicts import load_verdicts

    verdict_load = params.get("_verdicts")
    if verdict_load is None:
        try:
            verdict_load = load_verdicts(state_dir)
        except Exception:  # noqa: BLE001 — fail safe: retention only, no early delete
            from axiom.infra.artifact_verdicts import VerdictLoad

            verdict_load = VerdictLoad(unreachable=True)
    decisions: list = []
    art_del, art_keep = policy.select_artifacts(artifacts, now, verdicts=verdict_load, decisions=decisions)
    cache_del, cache_keep = policy.select_caches(caches, open_prs=open_prs, closed_or_merged=closed)

    reading = _reading(artifacts, caches, params, runner)
    crossings = classify_thresholds(reading)
    aggressive = reading.current_pct >= 85 or (reading.accrued_pct is not None and reading.accrued_pct >= 85)
    caps = params.get("caps") or blast_caps(
        reading.current_pct, settings_get, aggressive=aggressive
    )
    art_del, art_deferred = apply_blast_caps(art_del, caps)
    # Caches share the same item/byte remaining budget.
    remaining = type(caps)(
        max_items=max(0, caps.max_items - len(art_del)),
        max_gib=max(0.0, caps.max_gib - sum(a.size_bytes for a in art_del) / (1024**3)),
    )
    cache_del, cache_deferred = apply_blast_caps(cache_del, remaining)
    art_bytes = sum(a.size_bytes for a in art_del)
    cache_bytes = sum(c.size_bytes for c in cache_del)

    rate_ok, rate_reason = True, "ok"
    if act:
        rate_ok, rate_reason = rate_limit_allows(state_dir, now=now, settings_get=settings_get)
        if params.get("_skip_rate_limit"):
            rate_ok, rate_reason = True, "ok"
        if not rate_ok:
            act = False
            watch_only = True
            act_reason = rate_reason

    action = _action_line(
        watch_only=watch_only,
        art_n=len(art_del),
        art_b=art_bytes,
        cache_n=len(cache_del),
        cache_b=cache_bytes,
    )

    deleted_arts = 0
    deleted_caches = 0
    if act:
        for a in art_del:
            if provider.delete_artifact(a):
                deleted_arts += 1
        for c in cache_del:
            if hasattr(provider, "delete_cache") and provider.delete_cache(c):
                deleted_caches += 1
        if deleted_arts or deleted_caches:
            record_enforce_run(state_dir, now)

    recipients = _recipients(params)
    already: set[str] = set(params.get("_already_sent") or ())
    sender = params.get("_sender")
    alerts = [from_crossing(c, watch_only=watch_only, action_taken=action) for c in crossings]
    if deleted_arts or deleted_caches or (watch_only and (art_del or cache_del) and params.get("_announce_dry_run")):
        detail = (
            f"{action}. Recovery: {RECOVERY_ARTIFACT} {RECOVERY_CACHE}"
        )
        alerts.append(
            Alert(
                kind="storage_action",
                severity="high" if aggressive else "normal",
                summary=action,
                action_taken=action + f" Reason: {act_reason}.",
                decision=f"Recovery: {RECOVERY_ARTIFACT} {RECOVERY_CACHE}",
                dedup_key=f"tidy.storage.action.{now.date().isoformat()}",
                detail=detail,
            )
        )
    sent = send_alerts(alerts, recipients, already_sent=already, sender=sender) if recipients and alerts else []

    remaining_bytes = reading.artifact_bytes - (art_bytes if act else 0)
    remaining_pct = (remaining_bytes / (1024**3) / reading.included_shared_gb) * 100 if reading.included_shared_gb else 0.0
    needs_you = None
    if not watch_only and aggressive and remaining_pct >= 85:
        needs_you = file_needs_you(
            summary=(
                f"Actions shared storage is still {remaining_pct:.0f}% of the included cap "
                f"after in-policy reclaim ({remaining_bytes / (1024**3):.2f} GiB)."
            ),
            decision="Raise the cap, drop retention further, or approve a policy exception. TIDY will not touch the never-touch list.",
            detail=action,
            recipients=recipients,
            dedup_key=f"tidy.storage.still_over_85.{now.date().isoformat()}",
            sender=sender,
            state_dir=state_dir,
        )

    report = {
        "watch_only": watch_only,
        "act_reason": act_reason,
        "aggressive": aggressive,
        "repos": repos,
        "artifact_bytes": reading.artifact_bytes,
        "artifact_gb": round(reading.artifact_gb, 3),
        "current_pct": round(reading.current_pct, 1),
        "remaining_pct": round(remaining_pct, 1),
        "accrued_gb_hours": reading.accrued_gb_hours,
        "accrued_pct": None if reading.accrued_pct is None else round(reading.accrued_pct, 1),
        "cache_bytes_by_repo": reading.cache_bytes_by_repo,
        "caps": {"max_items": caps.max_items, "max_gib": caps.max_gib},
        "deferred": {
            "artifacts": len(art_deferred),
            "caches": len(cache_deferred),
        },
        "candidates": {
            "artifacts": len(art_del),
            "artifact_bytes": art_bytes,
            "caches": len(cache_del),
            "cache_bytes": cache_bytes,
        },
        "kept": {"artifacts": len(art_keep), "caches": len(cache_keep)},
        "deleted": {"artifacts": deleted_arts, "caches": deleted_caches},
        "recovery": {"artifacts": RECOVERY_ARTIFACT, "caches": RECOVERY_CACHE},
        "verdicts_unreachable": bool(getattr(verdict_load, "unreachable", False)),
        "rivet": [
            d for d in decisions
            if d.get("rivet_reason")
        ],
        "crossings": [
            {"kind": c.kind, "severity": c.severity, "pct": round(c.pct, 1), "summary": c.summary, "repo": c.repo}
            for c in crossings
        ],
        "alerts": sent,
        "needs_you": needs_you,
    }
    append_audit(
        state_dir,
        {
            "event": "storage_tick" if params.get("_scheduled") else "artifacts",
            "watch_only": watch_only,
            "act_reason": act_reason,
            "repos": repos,
            "current_pct": report["current_pct"],
            "remaining_pct": report["remaining_pct"],
            "candidates": report["candidates"],
            "deleted": report["deleted"],
            "deferred": report["deferred"],
            "recovery": report["recovery"],
            "verdicts_unreachable": report["verdicts_unreachable"],
            "rivet": report["rivet"],
            "crossings": [c["kind"] + ":" + c["severity"] for c in report["crossings"]],
        },
        now=now,
    )

    mode = "dry-run" if watch_only else "agency"
    lines = [
        f"TIDY Actions storage [{mode}: {act_reason}]: {reading.artifact_gb:.2f} GiB "
        f"({reading.current_pct:.0f}% of {reading.included_shared_gb:.0f} GB included).",
        action + ".",
        f"Recovery: {RECOVERY_ARTIFACT} {RECOVERY_CACHE}",
    ]
    if art_deferred or cache_deferred:
        lines.append(
            f"Deferred {len(art_deferred)} artifacts and {len(cache_deferred)} caches by this run's blast-radius cap "
            f"({caps.max_items} items / {caps.max_gib} GiB)."
        )
    rivet_notes = report["rivet"]
    if rivet_notes:
        kept_n = sum(1 for d in rivet_notes if d["action"] == "keep")
        del_n = sum(1 for d in rivet_notes if d["action"] == "delete")
        lines.append(
            f"RIVET verdicts: kept {kept_n} (even if past retention), "
            f"deleted {del_n} early; reasons: "
            + ", ".join(sorted({d['rivet_reason'] for d in rivet_notes}))
            + "."
        )
    if report["verdicts_unreachable"]:
        lines.append("RIVET verdict store unreachable — retention only, no early deletes.")
    for crossing in crossings:
        lines.append(f"{crossing.severity.upper()}: {crossing.summary}")
    if needs_you:
        lines.append("NEEDS YOU: storage is still over 85% after in-policy reclaim.")
    return SkillResult(ok=True, value=report, actions_taken=lines)


def artifacts(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi hygiene artifacts`` — acts by default; MCP stays dry-run unless enforce is set."""
    return run_storage(params, ctx)


def storage_tick(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """PULSE daily tick. Acts inside policy unless the kill switch or --dry-run is on."""
    params = dict(params)
    params["_scheduled"] = True
    return run_storage(params, ctx)


def storage_digest(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Weekly hygiene report through HERALD (always a report, never a delete)."""
    params = dict(params)
    params["watch_only"] = True
    params["_scheduled"] = True
    result = run_storage(params, ctx)
    if not result.ok or result.value is None:
        return result
    report = result.value
    summary = (
        f"TIDY weekly storage report: {report['artifact_gb']} GiB "
        f"({report['current_pct']}% of included). "
        f"Would delete {report['candidates']['artifacts']} artifacts and "
        f"{report['candidates']['caches']} caches."
    )
    recipients = params.get("recipients")
    if isinstance(recipients, str):
        recipients = tuple(part.strip() for part in recipients.split(",") if part.strip())
    elif isinstance(recipients, (list, tuple)):
        recipients = tuple(str(r) for r in recipients)
    else:
        recipients = ()
    if recipients:
        alert = Alert(
            kind="weekly_digest",
            severity="normal",
            summary=summary,
            action_taken="Compiled the weekly Actions storage report (no deletes).",
            decision="The daily tick already reclaims in-policy; approve a policy exception only if still over cap.",
            dedup_key="tidy.storage.weekly_digest",
            detail="\n".join(result.actions_taken),
        )
        send_alerts(
            [alert],
            recipients,
            already_sent=set(params.get("_already_sent") or ()),
            sender=params.get("_sender"),
        )
    result.actions_taken = [summary, *result.actions_taken]
    return result
