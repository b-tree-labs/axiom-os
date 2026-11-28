# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``hygiene.report`` — tiered draft, notification, package, and classify actions."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.hygiene.agency import decide_act, file_needs_you
from axiom.extensions.builtins.hygiene.classify import classify_candidates
from axiom.extensions.builtins.hygiene.github_reports import (
    DEFAULT_STALE_DAYS,
    NOTICE_LABEL,
    add_notice_label,
    close_pull,
    has_activity_after,
    is_bot_or_tidy,
    list_issue_comments,
    mark_notification_read,
    notice_posted_at,
    notification_report,
    package_report,
    post_stale_notice,
    stale_drafts,
    subject_is_merged_or_closed,
)
from axiom.extensions.builtins.hygiene.storage_alerts import Alert, send_alerts
from axiom.extensions.builtins.hygiene.storage_audit import append_audit
from axiom.infra.skills import SkillContext, SkillResult


def _repos(params: dict[str, Any]) -> list[str]:
    raw = params.get("repo") or params.get("repos")
    if isinstance(raw, str) and raw.strip():
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(part) for part in raw]
    env = os.environ.get("AXIOM_ACTIONS_REPOS") or ""
    if env.strip():
        return [part.strip() for part in env.split(",") if part.strip()]
    return []


def _recipients(params: dict[str, Any]) -> tuple[str, ...]:
    recipients = params.get("recipients")
    if isinstance(recipients, str):
        return tuple(part.strip() for part in recipients.split(",") if part.strip())
    if isinstance(recipients, (list, tuple)):
        return tuple(str(r) for r in recipients)
    return ()


def _act(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:
    params = dict(params)
    if not params.get("surface") and getattr(ctx, "surface", None):
        params["surface"] = ctx.surface
    return decide_act(params, settings_get=params.get("_settings_get"), scope="github")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _herald(params: dict[str, Any], summary: str, action_taken: str, decision: str, *, kind: str, dedup: str) -> None:
    recipients = _recipients(params)
    if not recipients:
        return
    alert = Alert(
        kind=kind,
        severity="normal",
        summary=summary,
        action_taken=action_taken,
        decision=decision,
        dedup_key=dedup,
    )
    send_alerts(
        [alert],
        recipients,
        already_sent=set(params.get("_already_sent") or ()),
        sender=params.get("_sender"),
    )


def _run_drafts(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    now = params.get("now") or datetime.now(timezone.utc)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    runner = params.get("_runner") or subprocess.run
    stale_days = int(params.get("stale_days") or DEFAULT_STALE_DAYS)
    act, act_reason = _act(params, ctx)
    repos = _repos(params)
    if not repos:
        return SkillResult(ok=False, errors=["no repo: pass --repo owner/name"])

    items: list[dict[str, Any]] = []
    for repo in repos:
        items.extend(stale_drafts(repo, now=now, stale_days=stale_days, runner=runner))

    noticed: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for item in items:
        repo = str(item["repo"])
        number = int(item["number"])
        comments = list_issue_comments(repo, number, runner=runner) if act or item.get("_comments") is not None else []
        if item.get("_comments") is not None:
            comments = list(item["_comments"])
        noticed_at = notice_posted_at(comments)
        already = noticed_at is not None or NOTICE_LABEL in (item.get("labels") or [])
        record = {**item, "noticed_at": noticed_at.isoformat() if noticed_at else None}

        if not act:
            skipped.append({**record, "would": "notice" if not already else "review-close"})
            continue

        if not already:
            ok_comment = post_stale_notice(repo, number, stale_days=stale_days, now=now, runner=runner)
            ok_label = add_notice_label(repo, number, runner=runner)
            if ok_comment or ok_label:
                noticed.append(record)
            continue

        if noticed_at is None:
            # Label is present but we could not parse a timestamp — do not close.
            skipped.append({**record, "would": "wait-for-notice-timestamp"})
            continue
        if now < noticed_at + timedelta(days=stale_days):
            skipped.append({**record, "would": "waiting-notice-period"})
            continue
        updated = _parse_dt(item.get("updated_at"))
        if has_activity_after(comments, noticed_at, updated_at=updated):
            skipped.append({**record, "would": "activity-after-notice"})
            continue

        bot = is_bot_or_tidy(item.get("author") or item.get("user"), item.get("author_type"))
        if bot:
            if close_pull(repo, number, runner=runner):
                closed.append(record)
            continue

        ask = file_needs_you(
            summary=f"Close human-authored stale draft {repo}#{number} ({item.get('title')})?",
            decision="TIDY posted a 7-day notice and the period expired with no activity. Approving lets TIDY close this human-authored PR.",
            detail=item.get("html_url") or "",
            recipients=_recipients(params),
            dedup_key=f"tidy.drafts.close_human.{repo}.{number}",
            sender=params.get("_sender"),
            state_dir=state_dir,
        )
        held.append({**record, "needs_you": ask})

    if act and (noticed or closed or held):
        summary = (
            f"TIDY draft hygiene: noticed {len(noticed)}, closed {len(closed)} "
            f"bot/TIDY drafts, held {len(held)} human drafts for approval."
        )
        _herald(
            params,
            summary,
            summary,
            "A closed bot draft can be reopened; a human draft was not closed. "
            "Comment or push to keep a noticed draft open.",
            kind="draft_hygiene",
            dedup=f"tidy.drafts.{now.date().isoformat()}",
        )
        append_audit(
            state_dir,
            {
                "event": "drafts",
                "act_reason": act_reason,
                "noticed": [f"{i['repo']}#{i['number']}" for i in noticed],
                "closed": [f"{i['repo']}#{i['number']}" for i in closed],
                "held": [f"{i['repo']}#{i['number']}" for i in held],
                "recovery": "Reopen a closed draft or push a commit to cancel a notice.",
            },
            now=now,
        )

    value = {
        "resource": "drafts",
        "stale_days": stale_days,
        "act": act,
        "act_reason": act_reason,
        "items": items,
        "count": len(items),
        "noticed": noticed,
        "closed": closed,
        "held": held,
        "skipped": skipped,
        "recovery": "Reopen a closed draft or push a commit to cancel a notice.",
    }
    lines = [
        f"Stale draft PRs (>{stale_days}d) [{('agency' if act else 'dry-run')}: {act_reason}]: {len(items)}",
    ]
    for item in items:
        author = item.get("author") or "unknown"
        lines.append(f"  {item['repo']}#{item['number']} {item['title']} by {author} (updated {item['updated_at']})")
    if not items:
        lines.append("  none")
    if act:
        lines.append(
            f"Noticed {len(noticed)}, closed {len(closed)} bot/TIDY drafts after notice, "
            f"held {len(held)} human drafts for approval."
        )
        lines.append("Recovery: reopen a closed draft, or comment/push to keep a noticed draft open.")
    else:
        lines.append("Dry-run — no comments, labels, or closes were applied.")
    return SkillResult(ok=True, value=value, actions_taken=lines)


def _run_notifications(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    now = params.get("now") or datetime.now(timezone.utc)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    runner = params.get("_runner") or subprocess.run
    act, act_reason = _act(params, ctx)
    value = notification_report(runner=runner)
    value["resource"] = "notifications"
    value["act"] = act
    value["act_reason"] = act_reason
    if not value.get("ok"):
        return SkillResult(ok=False, value=value, errors=[value.get("error") or "notifications unavailable"])

    marked: list[str] = []
    if act:
        for item in value.get("items") or []:
            url = item.get("subject_url") or ""
            closed = bool(item.get("_subject_closed")) if "_subject_closed" in item else subject_is_merged_or_closed(url, runner=runner)
            if not closed:
                continue
            if mark_notification_read(str(item.get("id") or ""), runner=runner):
                marked.append(str(item.get("id") or ""))
        if marked:
            summary = f"TIDY marked {len(marked)} GitHub notification(s) read (subject already merged or closed)."
            _herald(
                params,
                summary,
                summary,
                "Unneeded inbox noise was cleared. This is reversible only from GitHub's notification settings.",
                kind="notifications_read",
                dedup=f"tidy.notifications.{now.date().isoformat()}",
            )
            append_audit(
                state_dir,
                {
                    "event": "notifications",
                    "act_reason": act_reason,
                    "marked_read": marked,
                    "recovery": "Notification read-state is not restored automatically.",
                },
                now=now,
            )
    value["marked_read"] = marked

    lines = [
        f"Unread GitHub notifications [{('agency' if act else 'dry-run')}: {act_reason}]: {value['unread']}",
    ]
    for item in value.get("items") or []:
        lines.append(f"  {item.get('repository')}: {item.get('title')} ({item.get('reason')})")
    if act:
        lines.append(f"Marked {len(marked)} merged/closed notification(s) read.")
    else:
        lines.append("Dry-run — no notifications were marked read.")
    return SkillResult(ok=True, value=value, actions_taken=lines)


def _run_packages(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    org = params.get("org") or os.environ.get("AXIOM_STORAGE_ORG") or ""
    if not org:
        return SkillResult(ok=False, errors=["no org: pass --org name (Packages share the Actions storage cap)"])
    runner = params.get("_runner") or subprocess.run
    value = package_report(org, runner=runner)
    value["resource"] = "packages"
    lines = [f"GitHub Packages for {org}: {len(value.get('packages') or [])}"]
    for pkg in value.get("packages") or []:
        lines.append(f"  {pkg.get('package_type')}/{pkg.get('name')} ({pkg.get('visibility')})")
    for err in value.get("errors") or []:
        lines.append(f"  note: {err}")
    lines.append("Packages are on the never-touch list — deleting one is a tier 2 Needs you approval.")
    return SkillResult(ok=True, value=value, actions_taken=lines, errors=list(value.get("errors") or []))


def _run_classify(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    now = params.get("now") or datetime.now(timezone.utc)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    candidates = params.get("candidates") or []
    if not isinstance(candidates, list):
        return SkillResult(ok=False, errors=["candidates must be a list"])
    results = classify_candidates(
        candidates,
        complete=params.get("_complete"),
        completer=params.get("_completer"),
    )
    proposed = []
    act, act_reason = _act(params, ctx)
    if act:
        for c in results:
            if c.decision != "approval":
                continue
            ask = file_needs_you(
                summary=f"TIDY classifier proposes a tier 2 action for {c.id}",
                decision=c.rationale or "The leftover is irreversible, unclear, or outside policy.",
                detail=f"decision={c.decision} source={c.source}",
                recipients=_recipients(params),
                dedup_key=f"tidy.classify.tier2.{c.id}",
                sender=params.get("_sender"),
                state_dir=state_dir,
            )
            proposed.append({"id": c.id, "needs_you": ask})
        if proposed:
            append_audit(
                state_dir,
                {
                    "event": "classify",
                    "act_reason": act_reason,
                    "proposed": [p["id"] for p in proposed],
                    "label_only": True,
                },
                now=now,
            )
    value = {
        "resource": "classify",
        "items": [{"id": c.id, "decision": c.decision, "rationale": c.rationale, "source": c.source} for c in results],
        "count": len(results),
        "tier2_proposed": proposed,
        "act": act,
        "act_reason": act_reason,
    }
    lines = [f"Classified {len(results)} hygiene leftover(s). Labels only — no deletes from the classifier."]
    for c in results:
        lines.append(f"  {c.id}: {c.decision} — {c.rationale}")
    if not results:
        lines.append("  (no model configured, or nothing to classify — deterministic policy still applies)")
    if proposed:
        lines.append(f"Filed {len(proposed)} tier 2 Needs you approval request(s).")
    return SkillResult(ok=True, value=value, actions_taken=lines)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi hygiene report <drafts|notifications|packages|classify>``."""
    resource = params.get("resource") or "drafts"
    if resource == "drafts":
        return _run_drafts(params, ctx)
    if resource == "notifications":
        return _run_notifications(params, ctx)
    if resource == "packages":
        return _run_packages(params, ctx)
    if resource == "classify":
        return _run_classify(params, ctx)
    if resource == "tick":
        return github_tick(params, ctx)
    return SkillResult(
        ok=False,
        errors=[f"unknown report resource {resource!r}; supported: drafts, notifications, packages, classify"],
    )


def github_tick(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """PULSE tick: act on stale drafts and merged/closed notifications, then digest."""
    params = dict(params)
    drafts = _run_drafts(params, ctx)
    notes = _run_notifications(params, ctx)
    ok = drafts.ok and notes.ok
    dval = drafts.value or {}
    nval = notes.value or {}
    summary = (
        f"TIDY GitHub hygiene digest: noticed {len(dval.get('noticed') or [])} drafts, "
        f"closed {len(dval.get('closed') or [])}, held {len(dval.get('held') or [])} for approval, "
        f"marked {len(nval.get('marked_read') or [])} notifications read."
    )
    _herald(
        params,
        summary,
        summary,
        "See the audit log for each action. Human PR closes and package deletes stay with you.",
        kind="github_digest",
        dedup=f"tidy.github.digest.{(params.get('now') or datetime.now(timezone.utc)).date().isoformat()}",
    )
    lines = [summary, *drafts.actions_taken, *notes.actions_taken]
    errors = list(drafts.errors) + list(notes.errors)
    return SkillResult(
        ok=ok,
        value={"resource": "tick", "drafts": dval, "notifications": nval, "digest": summary},
        actions_taken=lines,
        errors=errors,
    )
