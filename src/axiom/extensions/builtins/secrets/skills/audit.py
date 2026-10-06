# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.audit`` — expiry findings over foreign-credential metadata.

Pure metadata pass: expired / expiring (within ``within_days``, default
14) / ok / no_expiry, per credential. This is the deterministic surface
KEEP's future proactive-rotation scheduling reads (follow-up issue);
here it only reports. Values are never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore

DEFAULT_WITHIN_DAYS = 14


def _parse_when(raw: str) -> datetime | None:
    text = raw.strip()
    for parse in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
    ):
        try:
            when = parse(text)
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=UTC)
    return None


def audit_findings(
    store: ForeignCredentialStore,
    *,
    within_days: int = DEFAULT_WITHIN_DAYS,
    now: datetime | None = None,
) -> list[dict]:
    """One finding per credential — the shared core for CLI + MCP."""
    current = now or datetime.now(UTC)
    horizon = current + timedelta(days=within_days)
    findings: list[dict] = []
    for meta in store.list():
        name = meta["name"]
        raw = meta.get("expires_at")
        if not raw:
            findings.append({
                "name": name, "level": "no_expiry", "expires_at": None,
                "detail": "no expiry recorded; set one so KEEP can audit it",
            })
            continue
        when = _parse_when(str(raw))
        if when is None:
            findings.append({
                "name": name, "level": "unparseable", "expires_at": raw,
                "detail": "expires_at is not ISO-8601; fix the metadata",
            })
            continue
        if when <= current:
            level, detail = "expired", "credential expiry has passed — rotate now"
        elif when <= horizon:
            level = "expiring"
            detail = (
                f"expires within {within_days} days — "
                f"rotate with `axi secrets rotate {name}`"
            )
        else:
            level, detail = "ok", "expiry beyond the audit horizon"
        findings.append({
            "name": name, "level": level, "expires_at": raw, "detail": detail,
        })
    return findings


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    within = int(params.get("within_days", DEFAULT_WITHIN_DAYS))
    now = None
    if params.get("_now"):
        now = _parse_when(str(params["_now"]))
    try:
        findings = audit_findings(store, within_days=within, now=now)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"audit failed: {exc}"])

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1
    return SkillResult(
        ok=True,
        value={
            "within_days": within,
            "findings": findings,
            "counts": counts,
        },
    )


__all__ = ["run", "audit_findings", "DEFAULT_WITHIN_DAYS"]
